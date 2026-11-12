"""0011：预览任务落库（v5 一期·步骤 6，规格 .scratch/v5-ingest-fix/spec.md §3）。

`preview_jobs` 表：进程内单 worker 执行器的持久底座——重启续跑（payload_json
携带重建 work 闭包所需参数，图从凭证 file_path 取）、进度节流落库、终态必落。
queue_position 不落列（按 created_at 查询时算）。

无 CLAIM 标记（v5 新增面，首跑也实际执行）；全 IF NOT EXISTS 幂等。
清理：done/error 且 finished_at > 7 天由 scheduler 日任务删行（queued/running 永不清）。
"""


def upgrade(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS preview_jobs ("
        "  job_id TEXT PRIMARY KEY,"
        "  status TEXT NOT NULL DEFAULT 'queued',"      # queued | running | done | error
        "  stage TEXT,"                                   # queued|cache|ocr|organize|match|done
        "  ocr_json TEXT,"                                # {chunk_index,chunk_total,line_done,line_total}
        "  lines_json TEXT,"                              # [{t,s,c}]，cap 1000（同内存）
        "  result_json TEXT,"                             # done 终态：与 POST /preview 同形响应
        "  error TEXT,"                                   # error 终态原因
        "  payload_json TEXT NOT NULL,"                   # 续跑参数：evidence_id/sha/source_group/
        #   unrecognized/filename/force_ocr/engine（tempfile 不可持久→从凭证取图）
        "  created_at TEXT NOT NULL,"
        "  started_at TEXT,"
        "  finished_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_preview_jobs_status_created"
        " ON preview_jobs (status, created_at)"
    )
