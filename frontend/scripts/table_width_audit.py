#!/usr/bin/env python3
"""表格列宽静态审计：写死像素 / minWidth / hideBelow / 档位漂移 / 已知缺陷复算。

对应 spec `.scratch/table-width/spec.md` C3（`table_width_audit.py` 与 `tokens:check`
必须进 `ci.sh`，否则档值漂移无人发现）+ A1/A2/A3 + `.scratch/table-width-impl/issues/03`
范围第 4 条。

三层判据里的**静态层**（浏览器层在 scripts/table_width_probe.js）：
  · A1 列定义不再出现数值 `width`（`width: 104`）——写死像素是现状 7 列不足的成因；
  · A2 `minWidth=` 外部传参归零（表宽改由 DataTable 内部按 Σ档位派生）；
  · A3 `hideBelow` 归零（<768 卡片之外，列不再被整列移出 DOM）；
  · 档位漂移：`widthTier` 落到 tokens 的 `table.col.*` 且档值 ≥ 该列「最坏内容」口径；
  · **已知缺陷复算**（`--expect-known-defects`）：把 spec §1.3 的 9 行按
    `溢出 = 最坏内容 − (声明宽度 − 内边距)` 复算一遍，值必须与 spec 记录一致。

为什么需要静态层：spec §1.3 的 9 行里，有 4 行**在渲染层测不到** ——
  · 记录·操作列 21.0px：列宽赤字（236 vs 需要 241.02），渲染时按钮被 flex 压小而非文本溢出；
  · 当前值·状态胶囊 4.1px：`label` 档最坏内容 114.05 来自「已使用 · 1,024 行」，而状态列
    值域只有「最新/待更新/未录入」，只读渲染撑不到；
  · 凭证·缩略图 12px / 使用情况 24.1px：凭证表 `minWidth=1000` > 声明列和 740，列被等比
    放大（03-report §B.3 坑④），声明宽度不足被放大遮住了。
浏览器层测得到的 5 行由 scripts/table_width_probe.js 负责，两层合起来才算「漏检率 0」。

用法：
    python3 frontend/scripts/table_width_audit.py                    # 审计（有发现即 exit 1）
    python3 frontend/scripts/table_width_audit.py --allow-pending     # 票 01/02 未落地时 exit 0（PENDING）
    python3 frontend/scripts/table_width_audit.py --expect-known-defects   # 反向验证：9 行必须复算命中
    python3 frontend/scripts/table_width_audit.py --emit-ellipsis-allowlist PATH
    python3 frontend/scripts/table_width_audit.py --json PATH
退出码：0 通过（或 PENDING 放行）；1 有发现/复算不符；2 环境问题（缺文件、解析不出列模型）。

省略白名单（`--emit-ellipsis-allowlist`）的抽取粒度（spec §6.3 R3.6）：**每张表各自登记**，
按 `<DataTable label="…" columns={…}>` 的 label 建键、按该 columns 数组里的 `ellipsis: true`
取值。同一文件里的第二张表（支出模板·备注、导出历史·文件名）因此也能登记 ——
这是**登记能力的扩充**，不是判据放宽：合法省略的双条件（配置 `ellipsis: true` +
DOM `title`）一字未动，探针 `legalEllipsis()` 仍然两条都查。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"

# 12 个列组所在的源文件（spec D5；8 个 DataTable 组 + 4 个原生表组，共落在 8 个文件里）。
GROUP_FILES = [
    ("records", "src/app/records/page.tsx"),
    ("records-current", "src/components/records/CurrentValueTable.tsx"),
    ("expenses", "src/app/expenses/page.tsx"),
    ("evidence", "src/app/data/evidence/page.tsx"),
    ("export", "src/app/export/page.tsx"),
    ("analysis", "src/app/analysis/page.tsx"),
    ("import", "src/app/import/page.tsx"),
    ("allocation", "src/components/allocation/AnalysisTable.tsx"),
]

# 列定义所在的 DataTable 组件（选择列 w-12 / px-3 写在这里）。
DATATABLE = "src/components/ui/DataTable.tsx"

# 每列水平内边距（px）：DataTable 数据格 `px-4 tablet:px-5` → ≥1024 时 40；操作列 `px-2` → 16。
PAD_DATA_CELL = 40
PAD_ACTIONS_CELL = 16

# 「最坏内容」单行宽度（px）。来源：`.scratch/table-width/01-report.md` §1.2 / §3.4 的实测值，
# 每一项都留了可复算的推导（谁改档值先看这行字）。
BASIS = {
    "datetime": (95.36, "2026/11/10 14:32（16 字符 @14px，§1.2 datetime 档）"),
    "amount-db-max": (89.39, "¥1,392,594.36（现库最大金额，§3.4）"),
    "amount-liability": (134.92, "¥999,999,999.99 + 6 gap + 24「负债」（§1.2 amount-extra 档）"),
    "label-pill": (114.05, "已使用 · 1,024 行 + 胶囊 16（§1.2 label 档）"),
    "classification-pill": (97.39, "补分类 · 缺 N + 铅笔 11 + 胶囊 16（§3.4）"),
    "actions-record": (241.02, "「进入资产项」94.02 + 44×3 图标 + gap 12 + 内边距（§3.4）"),
    "thumb": (100.0, "100px 缩略图定值（不可缩，§1.2 thumb 档）"),
    "select-hit": (44.0, "44px 复选框命中区（--pf-control-hit，§1.2 select 档）"),
}

# spec §1.3 的 9 行已知缺陷：声明宽度从源码解析，溢出量与 spec 记录值比对。
KNOWN_DEFECTS = [
    {
        "id": "records-updated-at", "file": "records", "key": "updated_at", "header": "更新时间",
        "basis": "datetime", "pad": PAD_DATA_CELL, "spec_over": 7.4, "tol": 0.2,
        "declared": ("width", 128),
    },
    {
        "id": "records-amount", "file": "records", "key": "amount", "header": "金额（元）",
        "basis": "amount-db-max", "pad": PAD_DATA_CELL, "spec_over": 25.4, "tol": 0.2,
        "declared": ("width", 104),
    },
    {
        "id": "records-amount-liability", "file": "records", "key": "amount", "header": "金额（元）",
        "basis": "amount-liability", "pad": PAD_DATA_CELL, "spec_over": 70.9, "tol": 0.2,
        "declared": ("width", 104),
    },
    {
        "id": "records-classification", "file": "records", "key": "classification_missing", "header": "分类",
        "basis": "classification-pill", "pad": PAD_DATA_CELL, "spec_over": 37.4, "tol": 0.2,
        "declared": ("width", 100),
    },
    {
        "id": "records-actions", "file": "records", "prop": "rowActionsWidth",
        "basis": "actions-record", "pad": PAD_ACTIONS_CELL, "spec_over": 21.0, "tol": 0.2,
        "declared": ("width", 236),
    },
    {
        "id": "records-current-status", "file": "records-current", "key": "status", "header": "状态",
        "basis": "label-pill", "pad": PAD_DATA_CELL, "spec_over": 4.1, "tol": 0.2,
        "declared": ("width", 150),
    },
    {
        "id": "evidence-thumb", "file": "evidence", "key": "thumbnail", "header": "缩略图",
        "basis": "thumb", "pad": PAD_DATA_CELL, "spec_over": 12, "tol": 0.2,
        "declared": ("width", 128),
    },
    {
        "id": "evidence-usage", "file": "evidence", "key": "usage", "header": "使用情况",
        "basis": "label-pill", "pad": PAD_DATA_CELL, "spec_over": 24.1, "tol": 0.2,
        "declared": ("width", 130),
    },
    {
        "id": "records-select-hit", "file": "datatable", "className": "w-12 px-3",
        "basis": "select-hit", "pad": 24, "spec_over": 20, "tol": 0.2,
        "declared": ("width", 48),
    },
]


@dataclass
class Column:
    """一列列定义里我们关心的字段（其余字段与验收无关，不解析）。"""

    key: str | None = None
    header: str | None = None
    width: int | None = None
    width_tier: str | None = None
    hide_below: str | None = None
    ellipsis: bool = False
    line: int = 0
    raw: str = ""


@dataclass
class FileAudit:
    name: str
    path: str
    exists: bool = True
    columns: list[Column] = field(default_factory=list)
    numeric_widths: list[dict] = field(default_factory=list)
    min_widths: list[dict] = field(default_factory=list)
    hide_belows: list[dict] = field(default_factory=list)
    has_width_tier: bool = False
    row_actions_width: int | None = None


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _match_object(text: str, start: int) -> str:
    """从 `start`（指向 `{` 之后）扫到配对的 `}`，跳过字符串/模板/注释。"""
    depth = 1
    i = start
    n = len(text)
    while i < n and depth > 0:
        ch = text[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    break
                i += 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return text[start:i]


COLUMN_MARKERS = ("key:", "header:", "cell:")


def parse_columns(text: str) -> list[Column]:
    """按 `key: 'x'` 切列对象（列数组是逐页内联的字面量，只能就地切）。"""
    out: list[Column] = []
    for m in re.finditer(r"\bkey:\s*'([^']+)'", text):
        line = text.count("\n", 0, m.start()) + 1
        body = _match_object(text, m.end())
        # 对象体：从 key 之后到配对 } —— 够拿到同列的 header/width 等字段。
        header_m = re.search(r"\bheader:\s*'([^']*)'", body) or re.search(r'\bheader:\s*"([^"]*)"', body)
        width_m = re.search(r"\bwidth:\s*(\d+)", body)
        tier_m = re.search(r"\bwidthTier:\s*'([a-z-]+)'", body)
        hide_m = re.search(r"\bhideBelow:\s*'(lg|xl)'", body)
        ellipsis_m = re.search(r"\bellipsis:\s*true", body)
        col = Column(
            key=m.group(1),
            header=header_m.group(1) if header_m else None,
            width=int(width_m.group(1)) if width_m else None,
            width_tier=tier_m.group(1) if tier_m else None,
            hide_below=hide_m.group(1) if hide_m else None,
            ellipsis=bool(ellipsis_m),
            line=line,
            raw=body[:400],
        )
        # 只留真正的列定义：有 header / 数字宽度 / 档位 / cell 渲染函数之一。
        if col.header or col.width is not None or col.width_tier or "cell:" in body:
            out.append(col)
    return out


def _match_delimited(text: str, start: int, open_ch: str, close_ch: str) -> tuple[str, int]:
    """从 `start`（指向 `open_ch`）扫到配对的 `close_ch`，跳过字符串/模板字面量。

    返回 (含首尾括号的子串, 结束下标)。用于切 `columns={…}` 的花括号与 `= [ … ]` 的方括号。
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    break
                i += 1
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i
        i += 1
    return text[start:], n


def iter_datatable_usages(text: str) -> list[dict]:
    """抽出文件里每一处 `<DataTable …>` 的 `label` 与 `columns`（不改判据，只扩登记面）。

    JSX 开始标签用「花括号配对 + 字符串跳过」扫描，`toolbar={<div>…</div>}` 这种嵌套不会误切；
    `columns={xxx}` 再回文件里找 `const xxx: DataTableColumn<…>[] = [ … ]` 的数组字面量。
    找不到（label 非字面量 / columns 不是本地数组）就如实记进 `unresolved`，不猜。
    """
    out: list[dict] = []
    for m in re.finditer(r"<DataTable\b", text):
        i = m.end()
        # TS 泛型实参：`<DataTable<CurrentValueItem>` —— 必须先跳过，否则会被当成标签结束的 `>`。
        j = i
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j < len(text) and text[j] == "<":
            depth_angle = 0
            while j < len(text):
                ch = text[j]
                if ch == "<":
                    depth_angle += 1
                elif ch == ">":
                    depth_angle -= 1
                    if depth_angle == 0:
                        j += 1
                        break
                j += 1
            i = j
        depth = 0
        n = len(text)
        tag = ""
        while i < n:
            ch = text[i]
            if ch in "'\"`":
                quote = ch
                i += 1
                while i < n:
                    if text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == quote:
                        break
                    i += 1
            elif ch in "{([":
                depth += 1
            elif ch in "})]":
                depth -= 1
            elif ch == ">" and depth == 0:
                tag = text[m.end():i]
                break
            i += 1
        label = None
        lm = re.search(r'\blabel="([^"]*)"', tag)
        if lm:
            label = lm.group(1)
        else:
            lm = re.search(r"\blabel=\{\s*'([^']*)'\s*\}", tag) or re.search(
                r'\blabel=\{\s*"([^"]*)"\s*\}', tag)
            if lm:
                label = lm.group(1)
        line = text.count("\n", 0, m.start()) + 1
        cm = re.search(r"\bcolumns=\{", tag)
        array_text = ""
        columns_ref = None
        if cm:
            brace_start = m.end() + cm.end() - 1
            brace_text, _ = _match_delimited(text, brace_start, "{", "}")
            inner = brace_text[1:-1].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", inner):
                columns_ref = inner
                am = re.search(
                    rf"(?:const|let|var)\s+{re.escape(inner)}\b[^=]*=\s*", text)
                if am and am.end() < len(text) and text[am.end()] == "[":
                    array_text, _ = _match_delimited(text, am.end(), "[", "]")
            elif inner.startswith("["):
                array_text = inner
        out.append({
            "label": label,
            "columnsRef": columns_ref,
            "line": line,
            "columns": parse_columns(array_text) if array_text else [],
            "resolved": bool(label and array_text),
        })
    return out


def build_ellipsis_allowlist() -> tuple[dict[str, list[str]], list[dict]]:
    """`{表 aria-label: [声明了 ellipsis 的列名…]}` —— 每张 DataTable 各登记一份。

    扫描面是 `frontend/src/**/*.tsx` 里所有 `<DataTable>` 用法（不是硬编码表名清单），
    因此同文件多表、跨文件多表都会各自登记。返回 (白名单, 未解析的用法清单)。
    """
    allow: dict[str, list[str]] = {}
    unresolved: list[dict] = []
    src = FRONTEND / "src"
    for path in sorted(src.rglob("*.tsx")):
        if path.name == "DataTable.tsx" and path.parent.name == "ui":
            continue  # 组件定义自身，不是用法
        text = read(path)
        if "<DataTable" not in text:
            continue
        rel = str(path.relative_to(FRONTEND))
        for usage in iter_datatable_usages(text):
            if not usage["resolved"]:
                unresolved.append({"file": rel, "line": usage["line"],
                                   "label": usage["label"], "columns": usage["columnsRef"]})
                continue
            cols = [c.header for c in usage["columns"] if c.ellipsis and c.header]
            if cols:
                allow.setdefault(usage["label"], [])
                allow[usage["label"]] += [c for c in cols if c not in allow[usage["label"]]]
    return allow, unresolved


def audit_file(name: str, rel: str) -> FileAudit:
    path = FRONTEND / rel
    fa = FileAudit(name=name, path=rel)
    if not path.exists():
        fa.exists = False
        return fa
    text = read(path)
    fa.columns = parse_columns(text)
    fa.has_width_tier = "widthTier" in text
    for m in re.finditer(r"\bwidth:\s*(\d+)", text):
        fa.numeric_widths.append({"line": text.count("\n", 0, m.start()) + 1, "value": int(m.group(1))})
    # 外部 minWidth 传参（数字或变量都算：spec D3 要求这个 prop 整体消失）
    for m in re.finditer(r"\bminWidth=\{([^}]*)\}", text):
        fa.min_widths.append({
            "line": text.count("\n", 0, m.start()) + 1,
            "value": m.group(1).strip(),
        })
    for m in re.finditer(r"\bhideBelow\b", text):
        fa.hide_belows.append({"line": text.count("\n", 0, m.start()) + 1})
    raw_m = re.search(r"\browActionsWidth=\{(\d+)\}", text)
    if raw_m:
        fa.row_actions_width = int(raw_m.group(1))
    return fa


def pick_column(fa: FileAudit, kd: dict) -> Column | None:
    """按 key（+ header 消歧）挑列定义：同一 page 里 `key:` 也可能出现在非列对象里。"""
    cands = [c for c in fa.columns if c.key == kd.get("key")]
    if kd.get("header"):
        exact = [c for c in cands if c.header == kd["header"]]
        if exact:
            return exact[0]
    with_width = [c for c in cands if c.width is not None or c.width_tier]
    if with_width:
        return with_width[0]
    return cands[0] if cands else None


def load_tokens() -> dict:
    path = FRONTEND / "src/styles/tokens.json"
    if not path.exists():
        return {}
    data = json.loads(read(path))
    return ((data.get("table") or {}).get("col") or {})


def compute_known_defects(audits: dict[str, FileAudit], tiers: dict | None = None) -> list[dict]:
    """按 spec §1.3 的算术复算 9 行：溢出 = 最坏内容 − (声明宽度 − 内边距)。

    选择列有两种事实源，按「改造前后」自动切换，别写死一种：
      · 改造前：`className="w-12 px-3"` → 48px（spec §1.3 的 20px 越界就是按它算的）；
      · 改造后：`style={{ width: TIER.select }}` → 读 `tokens.table.col.select`（68）。
        此时内容盒 68−24=44 = 44px 命中区 → 溢出 0，即「该缺陷已修」而不是「解析不出来」。
    """
    tiers = tiers or {}
    rows = []
    for kd in KNOWN_DEFECTS:
        if kd["file"] == "datatable":
            path = FRONTEND / DATATABLE
            text = read(path) if path.exists() else ""
            m = re.search(r'className="w-12 px-3', text)
            select_tier = tiers.get("select")
            tier_m = re.search(r"width:\s*TIER\.select", text)
            if m:
                declared = 48
                line = text.count("\n", 0, m.start()) + 1
                source = f"{DATATABLE}:{line}"
            elif tier_m and select_tier:
                declared = int(select_tier)
                line = text.count("\n", 0, tier_m.start()) + 1
                source = f"{DATATABLE}:{line}（width: TIER.select → tokens.table.col.select={select_tier}）"
            else:
                declared = None
                line = None
                source = DATATABLE
        elif "prop" in kd:
            fa = audits[kd["file"]]
            declared = fa.row_actions_width
            line = None
            source = f"{fa.path}（{kd['prop']}=）"
        else:
            fa = audits[kd["file"]]
            col = pick_column(fa, kd)
            declared = col.width if col else None
            line = col.line if col else None
            source = f"{fa.path}:{line}" if line else fa.path
        required, basis_note = BASIS[kd["basis"]]
        pad = kd["pad"]
        if declared is None:
            rows.append({
                "id": kd["id"], "source": source, "declared": None,
                "contentBox": None, "required": required, "basis": basis_note,
                "over": None, "specOver": kd["spec_over"],
                "verdict": "UNPARSED", "hit": False,
            })
            continue
        content_box = declared - pad
        over = round(required - content_box, 2)
        hit = abs(over - kd["spec_over"]) <= kd["tol"]
        rows.append({
            "id": kd["id"], "source": source, "declared": declared,
            "pad": pad, "contentBox": content_box, "required": required, "basis": basis_note,
            "over": over, "specOver": kd["spec_over"], "delta": round(over - kd["spec_over"], 2),
            "verdict": "HIT" if hit else "MISMATCH", "hit": hit,
        })
    return rows


def audit(args) -> int:
    audits: dict[str, FileAudit] = {}
    for name, rel in GROUP_FILES:
        audits[name] = audit_file(name, rel)

    missing = [a.path for a in audits.values() if not a.exists]
    if missing:
        print(f"环境问题：找不到源文件 {missing}", file=sys.stderr)
        return 2

    tiers = load_tokens()
    migration_started = any(a.has_width_tier for a in audits.values())

    findings: list[dict] = []
    warn: list[dict] = []

    # A1 写死像素 / A2 minWidth / A3 hideBelow：数据域 12 列组所在文件里必须归零。
    for a in audits.values():
        for col in a.columns:
            if col.width is None:
                continue
            findings.append({
                "check": "A1", "file": a.path, "line": col.line,
                "message": f"列 `{col.key}`（{col.header or '无表头'}）残留数值 width: {col.width}"
                           f"（应改 widthTier，spec D2）",
            })
        for w in a.min_widths:
            findings.append({
                "check": "A2", "file": a.path, "line": w["line"],
                "message": f"残留 minWidth={{{w['value']}}}（表宽应由 DataTable 内部按 Σ档位派生，spec D3）",
            })
        for h in a.hide_belows:
            findings.append({
                "check": "A3", "file": a.path, "line": h["line"],
                "message": "残留 hideBelow（整列移出 DOM，spec D2/A3）",
            })

    # 档位漂移：widthTier → tokens.table.col 且档值 ≥ 最坏内容 + 内边距 + 2（§1.1 档值公式）。
    tier_budget = {}
    for name, px in tiers.items():
        tier_budget[name.replace("-", "_")] = px
    for kd in KNOWN_DEFECTS:
        if kd["file"] == "datatable" or "prop" in kd:
            continue
        fa = audits[kd["file"]]
        col = pick_column(fa, kd)
        if not col or not col.width_tier:
            continue
        px = tiers.get(col.width_tier.replace("_", "-")) or tiers.get(col.width_tier)
        required = BASIS[kd["basis"]][0]
        if px is None:
            findings.append({
                "check": "C3", "file": fa.path, "line": col.line,
                "message": f"widthTier='{col.width_tier}' 在 tokens.table.col 里不存在",
            })
        elif px - PAD_DATA_CELL < required - 0.01:
            findings.append({
                "check": "C3", "file": fa.path, "line": col.line,
                "message": f"档位 {col.width_tier}={px} 的内容盒 {px - PAD_DATA_CELL} < 最坏内容 {required}（档值漂移）",
            })

    known = compute_known_defects(audits, tiers)

    # 省略白名单：**每张表各自登记**（spec §6.3 R3.6）。不再按文件「第一张表」硬编码。
    ellipsis, ellipsis_unresolved = build_ellipsis_allowlist()

    result = {
        # 相对仓库根（不写绝对路径：check-public-safety.sh 会拦 /srv、/home/<user>）
        "root": ".",
        "migrationStarted": migration_started,
        "tierBudget": tier_budget,
        "files": [
            {
                "name": a.name, "path": a.path,
                "columns": len(a.columns),
                "numericWidths": a.numeric_widths,
                "minWidths": a.min_widths,
                "hideBelows": a.hide_belows,
                "hasWidthTier": a.has_width_tier,
                "rowActionsWidth": a.row_actions_width,
                "ellipsisColumns": [c.header for c in a.columns if c.ellipsis and c.header],
            }
            for a in audits.values()
        ],
        "knownDefects": known,
        "ellipsisAllowlist": ellipsis,
        "ellipsisUnresolved": ellipsis_unresolved,
        "findings": findings,
        "warnings": warn,
        "summary": {
            "A1": sum(1 for f in findings if f["check"] == "A1"),
            "A2": sum(1 for f in findings if f["check"] == "A2"),
            "A3": sum(1 for f in findings if f["check"] == "A3"),
            "C3": sum(1 for f in findings if f["check"] == "C3"),
            "knownDefectsHit": sum(1 for k in known if k["hit"]),
            "knownDefectsTotal": len(known),
        },
    }

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.emit_ellipsis_allowlist:
        Path(args.emit_ellipsis_allowlist).parent.mkdir(parents=True, exist_ok=True)
        Path(args.emit_ellipsis_allowlist).write_text(
            json.dumps(ellipsis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ---------- 反向验证模式：9 行必须全部复算命中 ----------
    if args.expect_known_defects:
        print(f"反向验证（spec §1.3 的 {len(known)} 行已知缺陷复算）")
        print(f"{'缺陷':34s} {'声明宽':>7s} {'内容盒':>7s} {'最坏内容':>9s} {'溢出':>8s} {'spec':>7s}  判定")
        for k in known:
            print(f"{k['id']:34s} {str(k['declared']):>7s} {str(k['contentBox']):>7s} "
                  f"{k['required']:>9} {str(k['over']):>8s} {k['specOver']:>7}  {k['verdict']}")
        hits = sum(1 for k in known if k["hit"])
        print(f"\n命中 {hits}/{len(known)}")
        if hits != len(known):
            print("❌ 有已知缺陷复算不出来 —— 判据或 basis 常量有问题，不得进入 01/02 后的验收", file=sys.stderr)
            return 1
        print("✅ 9 行全部命中（静态层）")
        return 0

    # ---------- 常规审计 ----------
    allow_note = (f"省略白名单：{len(ellipsis)} 张表登记 "
                  f"{sum(len(v) for v in ellipsis.values())} 列"
                  + (f"（{len(ellipsis_unresolved)} 处用法 label/columns 非字面量未登记）"
                     if ellipsis_unresolved else ""))
    if not findings:
        print(f"✅ 静态审计通过：12 列组无写死 width / minWidth= / hideBelow，档位无漂移")
        print(f"   （{len(audits)} 个文件、{sum(len(a.columns) for a in audits.values())} 个列定义；"
              f"tokens.table.col {len(tiers)} 档；已知缺陷复算 "
              f"{result['summary']['knownDefectsHit']}/{len(known)} 行命中 spec 值）")
        print(f"   {allow_note}")
        return 0

    print(f"发现 {len(findings)} 处（A1={result['summary']['A1']} A2={result['summary']['A2']} "
          f"A3={result['summary']['A3']} C3={result['summary']['C3']}）：")
    for f in findings[:60]:
        print(f"  ❌ [{f['check']}] {f['file']}:{f['line']} {f['message']}")
    if len(findings) > 60:
        print(f"  … 其余 {len(findings) - 60} 处见 --json 输出")

    if args.allow_pending and not migration_started:
        print()
        print("⚠ PENDING：源码里还没有任何 widthTier，说明票 01/02 的档位改造尚未落地。")
        print("  本次按 --allow-pending 放行（默认档 CI 不因「尚未开工」变红）；")
        print("  改造一开始（出现第一个 widthTier）本段自动转为强制。")
        return 0

    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="表格列宽静态审计（A1/A2/A3 + 档位漂移 + 已知缺陷复算）")
    ap.add_argument("--json", default="", help="把完整结果写入该 JSON 路径")
    ap.add_argument("--emit-ellipsis-allowlist", default="",
                    help="把「ellipsis: true」的列按**每张表各自的 aria-label** 导出成 JSON"
                         "（喂给 table_width_probe.js；同文件多表各登记一份）")
    ap.add_argument("--expect-known-defects", action="store_true",
                    help="反向验证：spec §1.3 的 9 行必须全部复算命中（不符即 exit 1）")
    ap.add_argument("--allow-pending", action="store_true",
                    help="档位改造尚未开始时（源码无 widthTier）exit 0 并打 PENDING")
    args = ap.parse_args()
    return audit(args)


if __name__ == "__main__":
    raise SystemExit(main())