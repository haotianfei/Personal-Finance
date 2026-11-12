"""词表引擎单测（P1a）：出厂词表注册表 + 生效计算 + 库表读取 + 写入校验。

口径来源：`.scratch/parser-rules-refactor/spec.md` D2（四层规则来源）与 D2.2（全局可删留痕）。
所有词均为合成的演示词（示例新增词/示例组级词…），不含真实机构名或真实金额。
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import text

from errors import ApiError
from services import parser_rules as pr


# ---------------- 1. 注册表 ----------------

def test_registry_has_seven_immutable_keys():
    assert len(pr.VOCABULARY_KEYS) == 7
    assert set(pr.BUILTIN_VOCABULARIES) == set(pr.VOCABULARY_KEYS)
    for key in pr.VOCABULARY_KEYS:
        builtin = pr.BUILTIN_VOCABULARIES[key]
        assert isinstance(builtin, tuple), key
        assert builtin, key
        assert all(isinstance(w, str) and w for w in builtin), key

    # 模块级不暴露可变默认值：改 builtin 的“副本”不可能污染注册表
    assert isinstance(pr.BUILTIN_VOCABULARIES["ignore_words"], tuple)
    assert pr.BUILTIN_VOCABULARIES["row_close_tokens"] == (">", "›", "≫", "→")


# ---------------- 2. 无覆盖 = 出厂值（顺序也相等） ----------------

def test_no_overrides_equals_builtin_in_order():
    eff = pr.EffectiveVocabularies()
    for key in pr.VOCABULARY_KEYS:
        assert eff.get(key) == pr.BUILTIN_VOCABULARIES[key], key

    with pytest.raises(KeyError):
        eff.get("不存在的词表key")


# ---------------- 3. add：追加在末尾，不打断出厂顺序 ----------------

def test_add_appends_after_builtin_and_ignores_duplicates():
    builtin = pr.BUILTIN_VOCABULARIES["ignore_words"]
    eff = pr.EffectiveVocabularies(overrides={
        "ignore_words": (("示例新增词", builtin[0], "示例追加词"), ()),
    })
    got = eff.get("ignore_words")
    assert got[:len(builtin)] == builtin          # 出厂顺序逐项不变
    assert got[len(builtin):] == ("示例新增词", "示例追加词")  # 重复 add 被忽略
    assert len(got) == len(builtin) + 2


# ---------------- 4. remove / remove + add 并存 ----------------

def test_remove_drops_builtin_words():
    builtin = pr.BUILTIN_VOCABULARIES["amount_headers"]
    eff = pr.EffectiveVocabularies(overrides={"amount_headers": ((), (builtin[0],))})
    got = eff.get("amount_headers")
    assert builtin[0] not in got
    assert got == builtin[1:]


def test_remove_and_add_coexist():
    builtin = pr.BUILTIN_VOCABULARIES["total_labels"]
    eff = pr.EffectiveVocabularies(overrides={
        "total_labels": (("示例汇总词",), (builtin[-1],)),
    })
    got = eff.get("total_labels")
    assert got == builtin[:-1] + ("示例汇总词",)


# ---------------- 5. reset（空覆盖）回退出厂值 ----------------

def test_reset_restores_builtin():
    builtin = pr.BUILTIN_VOCABULARIES["name_exclude"]
    eff = pr.EffectiveVocabularies(overrides={"name_exclude": (("示例排除词",), (builtin[0],))})
    assert eff.get("name_exclude") != builtin

    reset = pr.EffectiveVocabularies(overrides={"name_exclude": ((), ())})
    assert reset.get("name_exclude") == builtin
    assert pr.EffectiveVocabularies(overrides={}).get("name_exclude") == builtin


# ---------------- 6. row_close_tokens 的 builtin 已去重 ----------------

def test_row_close_tokens_builtin_deduped():
    builtin = pr.BUILTIN_VOCABULARIES["row_close_tokens"]
    assert len(builtin) == len(set(builtin)) == 4
    assert builtin == (">", "›", "≫", "→")


# ---------------- 7. 组级覆盖：amount_header 替换 / 其余叠加 ----------------

def test_group_amount_header_is_replacement():
    base = pr.EffectiveVocabularies()
    # 组配置里的词即使同时是出厂词，替换后也必须原样保留（不能被 remove 抹空）
    replaced = base.with_group_overrides({"amount_header": ["持有金额", "示例表头"]})
    assert replaced.get("amount_headers") == ("持有金额", "示例表头")

    # 不改自身
    assert base.get("amount_headers") == pr.BUILTIN_VOCABULARIES["amount_headers"]

    # 组配置为空/全空白 → 视同未配置，回落出厂值（对齐 rules.get(...) or DEFAULT 语义）
    assert base.with_group_overrides({"amount_header": []}).get("amount_headers") \
        == pr.BUILTIN_VOCABULARIES["amount_headers"]
    assert base.with_group_overrides({"amount_header": ["  "]}).get("amount_headers") \
        == pr.BUILTIN_VOCABULARIES["amount_headers"]


def test_group_other_keys_are_additive():
    builtin = pr.BUILTIN_VOCABULARIES["ignore_words"]
    base = pr.EffectiveVocabularies(overrides={"ignore_words": (("示例全局词",), ())})
    merged = base.with_group_overrides({"ignore_words": ["示例组级词"]})

    got = merged.get("ignore_words")
    assert got[:len(builtin)] == builtin
    assert got[len(builtin):] == ("示例全局词", "示例组级词")

    # 无害键（当前组级无来源）不产生覆盖
    assert merged.with_group_overrides(None).get("ignore_words") == got
    assert merged.with_group_overrides({}).get("profit_zone") \
        == pr.BUILTIN_VOCABULARIES["profit_zone"]


# ---------------- 8. 表不存在 → 空覆盖（不抛） ----------------

def test_load_vocabularies_without_table(db):
    db.execute(text("DROP TABLE IF EXISTS parser_vocabularies"))
    db.commit()

    eff = pr.load_vocabularies(db)
    assert eff.overrides == {}
    for key in pr.VOCABULARY_KEYS:
        assert eff.get(key) == pr.BUILTIN_VOCABULARIES[key], key


# ---------------- 9. 表存在 → 读到 add/remove；坏行跳过 ----------------

def _insert_vocab_row(db, key, add_items, remove_items):
    """直插一行（对未知 schema 容错：表里有 updated_at 列时补当前时间）。"""
    cols = {row[1] for row in db.execute(text("PRAGMA table_info(parser_vocabularies)"))}
    columns = ["key", "add_items", "remove_items"]
    values = [":k", ":a", ":r"]
    params = {"k": key, "a": add_items, "r": remove_items}
    if "updated_at" in cols:  # 落库后的模型带 NOT NULL updated_at（Python 侧默认值）
        columns.append("updated_at")
        values.append(":ts")
        params["ts"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    db.execute(text(
        f"INSERT INTO parser_vocabularies ({', '.join(columns)})"
        f" VALUES ({', '.join(values)})"), params)


def test_load_vocabularies_reads_rows_and_skips_bad_json(db):
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS parser_vocabularies ("
        "key TEXT PRIMARY KEY, add_items TEXT, remove_items TEXT)"))
    _insert_vocab_row(db, "ignore_words", json.dumps(["示例新增词"]), json.dumps(["去诊断"]))
    # 坏 JSON 行 + 未知 key 行：都应被跳过且不影响其它行
    _insert_vocab_row(db, "noise_patterns", "{bad", "[]")
    _insert_vocab_row(db, "没有这个key", "[]", "[]")
    db.commit()

    eff = pr.load_vocabularies(db)
    builtin = pr.BUILTIN_VOCABULARIES["ignore_words"]
    got = eff.get("ignore_words")
    assert got[:len(builtin) - 1] == builtin[1:]  # 去诊断（首项）被 remove
    assert got[-1] == "示例新增词"
    assert "去诊断" not in got
    assert len(got) == len(builtin)

    assert "noise_patterns" not in eff.overrides
    assert "没有这个key" not in eff.overrides
    assert eff.get("profit_zone") == pr.BUILTIN_VOCABULARIES["profit_zone"]


# ---------------- 10. 写入校验 ----------------

def _assert_422(fn, *args):
    with pytest.raises(ApiError) as ei:
        fn(*args)
    assert ei.value.status_code == 422
    assert ei.value.code == "SOURCE_GROUP_RULES_INVALID"
    return ei.value.message


def test_validate_rejects_unknown_key():
    msg = _assert_422(pr.validate_vocabulary_payload, "没有这个key", [], [], None)
    assert "amount_headers" in msg and "ignore_words" in msg  # 报文含合法 key 列表


def test_validate_rejects_blank_or_non_string_items():
    _assert_422(pr.validate_vocabulary_payload, "ignore_words", ["示例词", ""], [], None)
    _assert_422(pr.validate_vocabulary_payload, "ignore_words", ["示例词", "   "], [], None)
    _assert_422(pr.validate_vocabulary_payload, "ignore_words", [123], [], None)
    _assert_422(pr.validate_vocabulary_payload, "ignore_words", [], [None], None)


def test_validate_rejects_add_remove_conflict():
    msg = _assert_422(pr.validate_vocabulary_payload, "ignore_words",
                      ["示例冲突词"], ["示例冲突词"], "说明")
    assert "示例冲突词" in msg


def test_validate_rejects_removing_word_not_in_effective_vocabulary():
    msg = _assert_422(pr.validate_vocabulary_payload, "ignore_words",
                      [], ["示例不存在的词"], "说明")
    assert "示例不存在的词" in msg


def test_validate_requires_reason_for_removal():
    builtin = pr.BUILTIN_VOCABULARIES["ignore_words"]  # 出厂词，可删但必须留理由
    _assert_422(pr.validate_vocabulary_payload, "ignore_words", [], [builtin[0]], None)
    _assert_422(pr.validate_vocabulary_payload, "ignore_words", [], [builtin[0]], "   ")


def test_validate_accepts_valid_payload():
    builtin = pr.BUILTIN_VOCABULARIES["amount_headers"]
    # 纯新增：不需要 reason；删出场词：需要 reason
    assert pr.validate_vocabulary_payload("amount_headers", ["示例新增词"], [], None) is None
    assert pr.validate_vocabulary_payload(
        "amount_headers", ["示例新增词"], [builtin[0]], "该词在本机所有 App 里都是噪声") is None
    assert pr.validate_vocabulary_payload("ignore_words", [], [], None) is None