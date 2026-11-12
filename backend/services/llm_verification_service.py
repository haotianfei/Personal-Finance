"""截图 AI 一致性校验服务（OpenAI 兼容视觉模型）。

流程：截图 bytes → base64 data URL → extract_items_from_image（LLM 视觉
识别资产清单）→ compare_verification（与前端现有 OCR 行比对，纯函数）。

依赖说明：项目 pyproject/requirements 均无 openai SDK（本机环境也未安装），
为避免新增重依赖，这里用手写 httpx POST /chat/completions（OpenAI 兼容
协议），extra_body 语义等价于请求体顶层字段合并。httpx 已随测试依赖安装。

verify 结果绝不落库（硬约束）：本模块只做 LLM 调用、内存/磁盘结果缓存
与纯比对逻辑，唯一持久化是 data/verification_cache/ 的识别结果缓存
（键含文件 sha256，与 ocr_cache 同构，性质是临时缓存而非业务数据）。
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import LLMProvider

# 视觉识别提示词：要求纯 JSON、amount 为字符串化十进制数（无货币符号）。
# response_format=json_object 要求顶层必须是 object，故让模型返回
# {"items": [...]}；解析端兼容裸数组 [{"name","amount"}] 两种形态。
PROMPT = (
    "请识别这张金融资产持仓截图中的每一项持仓资产。"
    "只输出一个合法的 JSON，不要任何解释、前后缀或 Markdown 代码块。"
    'JSON 格式为 {"items": [{"name": "产品名称", "amount": "金额"}]}，'
    "其中 items 是数组，每个元素只含 name 和 amount 两个字段；"
    "name 为产品完整名称；amount 为字符串化的十进制数（可含小数点，"
    "只算数字，不含货币符号、「元」或千分位逗号）；"
    "忽略总资产、收益、盈亏、按钮等非持仓行。"
)

_TIMEOUT_S = 60.0  # 视觉模型读密集小字较慢；不设 max_tokens（截断风险大于收益）


class LLMVerificationError(Exception):
    """LLM 调用/解析失败统一异常（超时、非 2xx、坏 JSON 等）。"""


# ---------------- 供应商预置 seed（照抄 seed_default_source_groups 模式） ----------------
# 预置一律 enabled=False / api_key="" / is_default=False：用户必须自行填 key
# 并启用后才能用于校验（未配置时 verify 全行降级 unverified）。
LLM_PROVIDER_SEEDS = [
    {"label": "DeepSeek", "kind": "deepseek",
     "base_url": "https://api.deepseek.com", "model": "deepseek-flash"},
    {"label": "百炼 Qwen", "kind": "dashscope",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "model": "qwen3-vl-plus"},
]


def seed_default_llm_providers(db: Session) -> None:
    """llm_providers 表无任何记录时预置两条（幂等；测试直接传 db 调用）。"""
    if db.execute(select(LLMProvider.id).limit(1)).first():
        return
    for seed in LLM_PROVIDER_SEEDS:
        db.add(LLMProvider(api_key="", is_default=False,
                           enabled=False, sort_order=0, **seed))
    db.commit()


# ---------------- C. OpenAI 兼容视觉客户端（手写 httpx） ----------------
def _endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def _build_request_body(provider: dict) -> dict:
    """按 provider kind 组装请求体（extra_body 语义 = 顶层字段合并）。

    - deepseek：关思考（thinking.type=disabled，官方 reasoning_effort 之外的
      稳妥写法；不支持该字段的版本会忽略，无副作用）。
    - dashscope：enable_thinking=False + vl_high_resolution_images=True
      （密集小字截图需高清模式）。
    - openai_compatible：仅 response_format，不带任何私有扩展。
    """
    body: dict = {
        "model": provider["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url",
                 "image_url": {"url": provider["_data_url"]}},
            ],
        }],
        "response_format": {"type": "json_object"},
    }
    kind = provider.get("kind")
    if kind == "deepseek":
        body["thinking"] = {"type": "disabled"}
    elif kind == "dashscope":
        body["enable_thinking"] = False
        body["vl_high_resolution_images"] = True
    return body


def _parse_completion(content: str) -> list[dict]:
    """模型回复文本 → [{name, amount}]；坏 JSON/坏结构抛 LLMVerificationError。

    兼容：{"items": [...]}、裸数组 [...]、```json 围栏。
    """
    text = (content or "").strip()
    # 剥 Markdown 代码块围栏（部分模型无视指令仍会包 ```json ... ```）
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text:
        raise LLMVerificationError("模型返回空内容")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMVerificationError(f"JSON 解析失败: {e}") from e
    if isinstance(data, dict):
        data = data.get("items")
    if not isinstance(data, list):
        raise LLMVerificationError("返回 JSON 不是 items 数组")
    items: list[dict] = []
    for it in data:
        if not isinstance(it, dict) or not isinstance(it.get("name"), str):
            raise LLMVerificationError("items 含非法元素")
        items.append({"name": it["name"],
                      "amount": str(it.get("amount", ""))})
    return items


def extract_items_from_image(image_b64_data_url: str, provider: dict) -> list[dict]:
    """截图 data URL → LLM 识别的资产清单 [{name, amount}]。

    provider 需含内部键：kind/base_url/api_key(明文)/model。
    任何异常（网络、超时、非 2xx、解析失败）统一抛 LLMVerificationError，
    由调用方降级为 unverified，绝不向上冒泡 5xx。
    """
    prov = dict(provider)
    prov["_data_url"] = image_b64_data_url
    headers = {
        "Authorization": f"Bearer {prov.get('api_key') or ''}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT_S) as client:
            resp = client.post(_endpoint(prov["base_url"]),
                               headers=headers, json=_build_request_body(prov))
        if resp.status_code // 100 != 2:
            raise LLMVerificationError(
                f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        content = payload["choices"][0]["message"]["content"]
    except LLMVerificationError:
        raise
    except (httpx.HTTPError, KeyError, IndexError, TypeError,
            ValueError, json.JSONDecodeError) as e:
        raise LLMVerificationError(f"LLM 调用失败: {e}") from e
    return _parse_completion(content)


# ---------------- 连通性测试（供应商设置页「测试」按钮） ----------------
_TEST_TIMEOUT_S = 10.0  # 比 verify 的 60s 短：连通测试要快速给出反馈


def test_provider_connection(provider: dict) -> dict:
    """向供应商发极简文本对话（ping / max_tokens=5，无图、无 response_format）。

    返回 {"ok": bool, "message": str, "elapsed_ms": int}，不抛异常：
    2xx → ok=True，message 取 choices[0].message.content（截 50 字符，无则
    "连接成功"）；非 2xx → ok=False，message "HTTP {status}: {error 摘要}"
    （响应体 message/error/detail 字段，截 200，无则 reason）；
    任何异常（超时/网络等）→ ok=False，message str(e) 截 200。
    elapsed_ms 一律 time.monotonic() 差值（毫秒）。
    """
    url = _endpoint(provider["base_url"])  # 复用：去尾斜杠 + /chat/completions
    headers = {
        "Authorization": f"Bearer {provider.get('api_key') or ''}",
        "Content-Type": "application/json",
    }
    body = {
        "model": provider["model"],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    t0 = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - t0) * 1000)

    try:
        with httpx.Client(timeout=_TEST_TIMEOUT_S) as client:
            resp = client.post(url, headers=headers, json=body)
        if resp.status_code // 100 == 2:
            message = "连接成功"
            try:
                content = resp.json()["choices"][0]["message"]["content"]
                message = (str(content).strip() or "连接成功")[:50]
            except Exception:
                pass  # 2xx 即算连通，回复解析失败不影响判定
            return {"ok": True, "message": message, "elapsed_ms": _elapsed_ms()}
        detail = ""
        try:
            data = resp.json()
            if isinstance(data, dict):
                err = data.get("error")
                err_msg = err.get("message") if isinstance(err, dict) else err
                detail = str(
                    data.get("message") or err_msg or data.get("detail") or "")
        except Exception:
            detail = getattr(resp, "text", "") or ""
        detail = (detail.strip() or getattr(resp, "reason_phrase", "") or "")[:200]
        return {"ok": False, "message": f"HTTP {resp.status_code}: {detail}",
                "elapsed_ms": _elapsed_ms()}
    except Exception as e:
        return {"ok": False, "message": str(e)[:200], "elapsed_ms": _elapsed_ms()}


# ---------------- D. 比对纯函数（无 IO，单测核心 seam） ----------------
# 归一化尾巴：括号尾巴（"（A类份额）"）与日期/期号尾巴（"20260819期"、
# "2026-08-19"、"2026年8月"）——OCR 名与 AI 名常见只差这些，折叠后比对。
_PAREN_TAIL_RE = re.compile(r"[（(][^（）()]*[）)]\s*$")
_DATE_TAIL_RE = re.compile(
    r"(20\d{2}([-/年]?\d{1,2}([-/月]?\d{1,2}日?)?)?(期|号)?|[-/]\d{1,2}[-/]\d{1,2})$")
_STRIP_CHARS = "「」｜|、·，。 \t"

_SIM_THRESHOLD = 0.85  # difflib 相似度阈值，≥ 视为同一项


def _norm_name(name: str) -> str:
    """名称归一化：剥括号尾巴 → 剥标点空白 → 日期/期号尾巴折叠为空。"""
    n = (name or "").strip()
    n = _PAREN_TAIL_RE.sub("", n)
    for ch in _STRIP_CHARS:
        n = n.replace(ch, "")
    # 反复剥尾（"xx基金(2026-08-19期)" 括号剥完后露出日期尾巴再剥）
    while True:
        m = _DATE_TAIL_RE.search(n)
        if not m or m.start() == 0:
            break
        n = n[:m.start()]
    return n


def _amount(s) -> Optional[Decimal]:
    """金额字符串 → Decimal（剥千分位/货币符号/元）；不可解析返回 None。"""
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace("，", "")
    t = t.strip("元￥¥$ ")
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _amount_eq(a, b) -> bool:
    da, db = _amount(a), _amount(b)
    if da is not None and db is not None:
        return da == db
    # 任一侧金额解析失败：退回字符串严格相等（保守判 mismatch）
    return str(a or "").strip() == str(b or "").strip()


def compare_verification(ocr_items: list[dict], llm_items: list[dict]) -> dict:
    """OCR 行 × LLM 识别项 → 逐行判定 + AI 独有项（纯函数，无 IO）。

    输入 ocr_items: [{"index": int, "name": str, "amount": str}]（前端现有行，
    index 原样回传）；llm_items: [{"name": str, "amount": str}]。

    匹配策略（每个 llm 项只用一次）：
      1) 归一化名精确匹配（先到先得）；
      2) 剩余行按 difflib SequenceMatcher 相似度 ≥ 0.85 贪心配对
         （按 OCR 行序，取相似度最高者，同分取靠前的 llm 项）。
    判定 verdict（英文稳定枚举，前端映射中文；unverified 由调用方降级填充）：
      match     名匹配且金额 Decimal 相等
      mismatch  名匹配/近似但金额不等
      ocr_only  OCR 有、AI 无对应项
    返回 {"rows": [{"index", "verdict", "ai_name", "ai_amount"}],
          "ai_only": [{"name", "amount"}]}。
    """
    # 规整输入：index 缺省用位置序（前端契约必带，此处防御）；字段宽松取用
    ocr: list[dict] = []
    for i, raw in enumerate(ocr_items or []):
        it = raw if isinstance(raw, dict) else {}
        idx = it.get("index")
        ocr.append({"index": i if idx is None else idx,
                    "name": str(it.get("name") or ""),
                    "amount": str(it.get("amount") if it.get("amount") is not None else "")})
    llm = [{"name": str(it.get("name") or ""),
            "amount": str(it.get("amount") if it.get("amount") is not None else "")}
           for it in (llm_items or []) if isinstance(it, dict)]
    used = [False] * len(llm)
    matched: dict[int, int] = {}  # ocr 位置 → llm 位置

    # 第一轮：归一化名精确（含原名精确）
    for i, o in enumerate(ocr):
        on = _norm_name(o["name"])
        for j, l in enumerate(llm):
            if used[j]:
                continue
            if on and on == _norm_name(l["name"]):
                used[j] = True
                matched[i] = j
                break

    # 第二轮：近似名贪心（按 OCR 行序，相似度最高且 ≥ 阈值配对）
    for i, o in enumerate(ocr):
        if i in matched:
            continue
        on = _norm_name(o["name"])
        if not on:
            continue
        best_j, best_ratio = -1, 0.0
        for j, l in enumerate(llm):
            if used[j]:
                continue
            ln = _norm_name(l["name"])
            if not ln:
                continue
            ratio = difflib.SequenceMatcher(None, on, ln).ratio()
            if ratio > best_ratio:
                best_j, best_ratio = j, ratio
        if best_j >= 0 and best_ratio >= _SIM_THRESHOLD:
            used[best_j] = True
            matched[i] = best_j

    rows = []
    for i, o in enumerate(ocr):
        j = matched.get(i)
        if j is None:
            rows.append({"index": o["index"], "verdict": "ocr_only",
                         "ai_name": None, "ai_amount": None})
        else:
            l = llm[j]
            rows.append({"index": o["index"],
                         "verdict": "match" if _amount_eq(o["amount"], l["amount"])
                         else "mismatch",
                         "ai_name": l["name"], "ai_amount": l["amount"]})
    ai_only = [dict(llm[j]) for j in range(len(llm)) if not used[j]]
    return {"rows": rows, "ai_only": ai_only}


# ---------------- E. 识别结果两级缓存（照抄 ocr_cache 双级模式） ----------------
_EXTRACT_CACHE: "OrderedDict[str, list[dict]]" = OrderedDict()
_EXTRACT_CACHE_MAX = 8

# 磁盘缓存目录：项目根/data/verification_cache（dirname 三级与 ocr_cache 一致）
_VCACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                           "data", "verification_cache")
_VCACHE_MAX_FILES = 20


def _cache_key(image_sha: str, provider: dict) -> str:
    """键 = 图片 sha256 + 供应商签名（id/model/kind）。

    纯图片 sha 会在用户切换供应商后错误复用旧模型的识别结果，
    故组合供应商签名；同图同供应商仍秒命中。
    """
    sig = hashlib.sha256(
        f"{provider.get('id')}|{provider.get('kind')}|{provider.get('model')}"
        .encode("utf-8")).hexdigest()[:16]
    return f"{image_sha}-{sig}"


def _cache_disk_read(key: str) -> Optional[list[dict]]:
    path = os.path.join(_VCACHE_DIR, f"{key}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, list) or any(
            not isinstance(it, dict) or not isinstance(it.get("name"), str)
            for it in data):
        return None
    return [{"name": it["name"], "amount": str(it.get("amount", ""))}
            for it in data]


def _cache_disk_write(key: str, items: list[dict]) -> None:
    """原子写（.tmp + os.replace），进程中途退出不会留半截 JSON。"""
    try:
        os.makedirs(_VCACHE_DIR, exist_ok=True)
        dst = os.path.join(_VCACHE_DIR, f"{key}.json")
        tmp = os.path.join(_VCACHE_DIR, f"{key}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, dst)
    except OSError:
        pass  # 缓存写失败不影响主流程


def _cache_disk_trim() -> None:
    if not os.path.isdir(_VCACHE_DIR):
        return
    files = sorted(
        (f for f in os.listdir(_VCACHE_DIR) if f.endswith(".json")),
        key=lambda f: os.path.getmtime(os.path.join(_VCACHE_DIR, f)),
    )
    for f in files[: max(0, len(files) - _VCACHE_MAX_FILES)]:
        try:
            os.remove(os.path.join(_VCACHE_DIR, f))
        except OSError:
            pass


def _cache_get(key: str) -> Optional[list[dict]]:
    hit = _EXTRACT_CACHE.get(key)
    if hit is not None:
        _EXTRACT_CACHE.move_to_end(key)
        return hit
    items = _cache_disk_read(key)
    if items is not None:
        _EXTRACT_CACHE[key] = items
        _EXTRACT_CACHE.move_to_end(key)
        while len(_EXTRACT_CACHE) > _EXTRACT_CACHE_MAX:
            _EXTRACT_CACHE.popitem(last=False)
    return items


def _cache_put(key: str, items: list[dict]) -> None:
    _EXTRACT_CACHE[key] = items
    _EXTRACT_CACHE.move_to_end(key)
    while len(_EXTRACT_CACHE) > _EXTRACT_CACHE_MAX:
        _EXTRACT_CACHE.popitem(last=False)
    _cache_disk_write(key, items)
    _cache_disk_trim()


# ---------------- E. 校验编排（无 DB 依赖，纯入参可测） ----------------
def _all_unverified(ocr_items: list[dict]) -> dict:
    """降级路径：无供应商/无 key/调用失败 → 全部行 unverified（不抛异常）。"""
    rows = []
    for i, raw in enumerate(ocr_items or []):
        idx = raw.get("index") if isinstance(raw, dict) else None
        rows.append({"index": i if idx is None else idx,
                     "verdict": "unverified", "ai_name": None, "ai_amount": None})
    return {"rows": rows, "ai_only": []}


def to_data_url(image_bytes: bytes, mime: str) -> str:
    """图片字节 → base64 data URL（JPEG/PNG，用文件 MIME）。"""
    if mime not in ("image/jpeg", "image/png"):
        mime = "image/png"  # 防御：未知类型退回 PNG（模型端会再嗅探）
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def verify_rows(ocr_items: list[dict], provider: Optional[dict],
                image_bytes: bytes, mime: str) -> dict:
    """校验编排：缓存查 → LLM 识别 → 比对。任何异常降级 200 + unverified。

    provider 为内部凭证 dict（含明文 api_key）或 None。结果不落库。
    """
    if not provider or not (provider.get("api_key") or "").strip():
        return _all_unverified(ocr_items)
    try:
        image_sha = hashlib.sha256(image_bytes).hexdigest()
        key = _cache_key(image_sha, provider)
        llm_items = _cache_get(key)
        if llm_items is None:
            try:
                llm_items = extract_items_from_image(
                    to_data_url(image_bytes, mime), provider)
            except LLMVerificationError:
                # 失败不缓存：下次重试，避免把瞬时故障固化
                return _all_unverified(ocr_items)
            _cache_put(key, llm_items)
        return compare_verification(ocr_items, llm_items)
    except Exception:
        # 硬约束兜底：verify 任何路径不得 5xx——意外异常同样降级 unverified
        return _all_unverified(ocr_items)
