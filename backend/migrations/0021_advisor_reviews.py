"""0021：复盘三表（`advisor_reviews` / `advisor_review_suggestions` /
`advisor_review_proposals`）＋写保护触发器。

规格：`.scratch/phase3-review/spec.md` v1.0 §4.2/附录 B；ADR-0014 D6/D7/D8。

- 复盘快照/锚点/目标列永不可改；`closed` 后结论/归因不可改；禁 DELETE。
- 建议批次 write-once；提案内容创建后不可改、终态不可再裁决、禁 DELETE；
  同复盘同 kind/subject/payload 至多一条 `proposed`（部分唯一索引）。
- FK 仅参考区 `goals/goal_versions` 与隔离区 `advisor_suggestions`；账本零 FK。
- 全 DDL IF NOT EXISTS；无 CLAIM、无 down；双跑幂等。
"""


def _create_tables(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS advisor_reviews ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  goal_id INTEGER NOT NULL REFERENCES goals (id),"
        "  goal_version_id INTEGER NOT NULL REFERENCES goal_versions (id),"
        "  start_goal_version_id INTEGER REFERENCES goal_versions (id),"
        "  as_of DATE NOT NULL,"
        "  start_as_of DATE,"
        "  label TEXT,"
        "  note TEXT,"
        "  status TEXT NOT NULL CHECK (status IN ('open','closed')),"
        "  snapshot_json TEXT NOT NULL,"
        "  snapshot_sha256 CHAR(64) NOT NULL,"
        "  attribution_json TEXT,"
        "  conclusion TEXT,"
        "  created_at TEXT NOT NULL,"
        "  closed_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_advisor_reviews_goal"
        " ON advisor_reviews (goal_id, as_of)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS advisor_review_suggestions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  review_id INTEGER NOT NULL REFERENCES advisor_reviews (id),"
        "  suggestion_id INTEGER NOT NULL REFERENCES advisor_suggestions (id),"
        "  UNIQUE (review_id, suggestion_id)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS advisor_review_proposals ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  review_id INTEGER NOT NULL REFERENCES advisor_reviews (id),"
        "  kind TEXT NOT NULL CHECK (kind IN"
        "    ('goal_version','threshold','allocation_target','narrative')),"
        "  subject_ref TEXT,"
        "  payload_json TEXT NOT NULL,"
        "  payload_sha256 CHAR(64) NOT NULL,"
        "  rationale TEXT NOT NULL,"
        "  status TEXT NOT NULL CHECK (status IN"
        "    ('proposed','adopted','dismissed')),"
        "  decided_at TEXT,"
        "  decision_note TEXT,"
        "  applied_kind TEXT,"
        "  applied_ref TEXT,"
        "  created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_advisor_review_proposal_content"
        " ON advisor_review_proposals"
        " (review_id, kind, COALESCE(subject_ref,''), payload_sha256)"
        " WHERE status = 'proposed'"
    )


def _create_triggers(conn):
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS advisor_reviews_snapshot_immutable"
        " BEFORE UPDATE ON advisor_reviews FOR EACH ROW"
        " WHEN NEW.snapshot_json IS NOT OLD.snapshot_json"
        "   OR NEW.snapshot_sha256 IS NOT OLD.snapshot_sha256"
        "   OR NEW.as_of IS NOT OLD.as_of"
        "   OR NEW.start_as_of IS NOT OLD.start_as_of"
        "   OR NEW.goal_id IS NOT OLD.goal_id"
        "   OR NEW.goal_version_id IS NOT OLD.goal_version_id"
        "   OR NEW.start_goal_version_id IS NOT OLD.start_goal_version_id"
        " BEGIN SELECT RAISE(ABORT, 'review snapshot is immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS advisor_reviews_closed_immutable"
        " BEFORE UPDATE ON advisor_reviews FOR EACH ROW"
        " WHEN OLD.status = 'closed' AND ("
        "      NEW.status IS NOT OLD.status"
        "   OR NEW.conclusion IS NOT OLD.conclusion"
        "   OR NEW.attribution_json IS NOT OLD.attribution_json)"
        " BEGIN SELECT RAISE(ABORT, 'closed review is immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS advisor_reviews_no_delete"
        " BEFORE DELETE ON advisor_reviews FOR EACH ROW"
        " BEGIN SELECT RAISE(ABORT, 'advisor_reviews does not allow DELETE'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS advisor_review_suggestions_no_update"
        " BEFORE UPDATE ON advisor_review_suggestions FOR EACH ROW"
        " BEGIN SELECT RAISE(ABORT, 'advisor_review_suggestions is write-once'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS advisor_review_suggestions_no_delete"
        " BEFORE DELETE ON advisor_review_suggestions FOR EACH ROW"
        " BEGIN SELECT RAISE(ABORT, 'advisor_review_suggestions is write-once'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS advisor_review_proposals_content_immutable"
        " BEFORE UPDATE ON advisor_review_proposals FOR EACH ROW"
        " WHEN NEW.review_id IS NOT OLD.review_id"
        "   OR NEW.kind IS NOT OLD.kind"
        "   OR COALESCE(NEW.subject_ref,'') <> COALESCE(OLD.subject_ref,'')"
        "   OR NEW.payload_json IS NOT OLD.payload_json"
        "   OR NEW.payload_sha256 IS NOT OLD.payload_sha256"
        "   OR NEW.rationale IS NOT OLD.rationale"
        " BEGIN SELECT RAISE(ABORT, 'review proposal content is immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS advisor_review_proposals_decided_immutable"
        " BEFORE UPDATE ON advisor_review_proposals FOR EACH ROW"
        " WHEN OLD.status <> 'proposed' AND ("
        "      NEW.status IS NOT OLD.status"
        "   OR NEW.decided_at IS NOT OLD.decided_at"
        "   OR NEW.decision_note IS NOT OLD.decision_note"
        "   OR NEW.applied_kind IS NOT OLD.applied_kind"
        "   OR NEW.applied_ref IS NOT OLD.applied_ref)"
        " BEGIN SELECT RAISE(ABORT, 'decided review proposal is immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS advisor_review_proposals_no_delete"
        " BEFORE DELETE ON advisor_review_proposals FOR EACH ROW"
        " BEGIN SELECT RAISE(ABORT, 'advisor_review_proposals does not allow delete');"
        " END"
    )


def upgrade(conn):
    _create_tables(conn)
    _create_triggers(conn)