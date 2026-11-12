import Link from 'next/link'
import { ArrowRight } from 'lucide-react'
import { formatDate, formatDateTime, formatFileSize, EMPTY_VALUE } from '@/lib/format'
import { EvidenceAffectedItems } from '@/components/audit/EvidenceAffectedItems'
import { EvidenceImage } from './EvidenceImage'
import { RecognitionSection } from './RecognitionSection'
import { MetaRow, engineLabel } from './shared'
import type { EvidenceImageExif, EvidenceImageMeta, EvidenceListItem } from '@/types'

/** EXIF 标量 → 展示文本：空串/非有限数/缺失一律 `null`（由调用方落「—」）。 */
function exifString(value: string | number | null | undefined): string | null {
  if (typeof value === 'string') {
    const trimmed = value.trim()
    return trimmed === '' ? null : trimmed
  }
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : null
  return null
}

/**
 * 尺寸 `宽 × 高`（ADR-0022 D2 的 width/height）；`image_meta` 为 null 或任一边
 * 缺失/非正数 → 「—」（spec §3-12：缺值不显示 undefined/null/NaN）。
 */
function imageSizeLabel(meta: EvidenceImageMeta | null | undefined): string {
  const w = meta?.width
  const h = meta?.height
  if (typeof w !== 'number' || typeof h !== 'number') return EMPTY_VALUE
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return EMPTY_VALUE
  return `${w} × ${h}`
}

/**
 * 拍摄时间：EXIF `DateTimeOriginal`（原样展示，如 `2026:10:30 23:22:13`）。
 * 截图常无 EXIF → 「—」；它与文件名里的上传时刻互不替代（ADR-0022 D1）。
 */
function shotAtLabel(exif: EvidenceImageExif | null | undefined): string {
  return exifString(exif?.DateTimeOriginal) ?? EMPTY_VALUE
}

/**
 * 设备：`Make + Model` 拼接；任一段缺失就只显有的那段，都缺 → 「—」。
 * 分开取而不是二选一，是因为 Android 常见 Make=`HUAWEI` / Model=`P40` 两段互补。
 */
function deviceLabel(exif: EvidenceImageExif | null | undefined): string {
  const parts = [exifString(exif?.Make), exifString(exif?.Model)]
  const present = parts.filter((part): part is string => part !== null)
  return present.length > 0 ? present.join(' ') : EMPTY_VALUE
}

/**
 * 凭证详情正文（record-provenance D8）：列表 Modal 与凭证详情页**共用这一份**。
 *
 * 纯展示为主：入参只有一条列表项（`GET /api/screenshots/evidence` 的行形状，
 * 详情端点同源构造，见 D7），组件自身无 fetch、无 state、无 `useEffect`。
 * 两处例外都是**下沉到子组件**的自有交互：`RecognitionSection` 的「查看识别行」，
 * 与票 02 的「受影响资产项」（`EvidenceAffectedItems`，点一项打开操作历史抽屉）。
 *
 * `image_meta` / `recognition_summary` 缺数据即 `null`，展示面一律落「—」。
 */
export function EvidenceDetail({
  item,
  onOpenItemHistory,
}: {
  item: EvidenceListItem
  /**
   * 在操作历史抽屉里复用本组件时，点「受影响资产项」由宿主换主体；
   * 不传则 `EvidenceAffectedItems` 自己开抽屉（凭证详情页 / 独立弹层）。
   */
  onOpenItemHistory?: (item: { item_id: number; display_name: string }) => void
}) {
  return (
    <div className="space-y-4">
      <div className="flex justify-center rounded-control border border-line bg-subtle p-2">
        <EvidenceImage
          id={item.id}
          alt={`凭证 #${item.id} 原图`}
          className="max-h-[55vh] w-auto max-w-full"
        />
      </div>

      <dl className="grid grid-cols-1 gap-x-4 gap-y-3 tablet:grid-cols-2">
        <MetaRow label="来源组">{item.source_group.label}（{item.source_group.slug}）</MetaRow>
        <MetaRow label="数据日期">
          <span className="pf-num">{formatDate(item.data_date)}</span>
        </MetaRow>
        <MetaRow label="识别引擎">{engineLabel(item.engine)}</MetaRow>
        <MetaRow label="上传时间">
          <span className="pf-num">{formatDateTime(item.created_at)}</span>
        </MetaRow>
        <MetaRow label="尺寸">
          <span className="pf-num">{imageSizeLabel(item.image_meta)}</span>
        </MetaRow>
        <MetaRow label="拍摄时间">
          <span className="pf-num">{shotAtLabel(item.image_meta?.exif)}</span>
        </MetaRow>
        <MetaRow label="设备">{deviceLabel(item.image_meta?.exif)}</MetaRow>
        <MetaRow label="文件大小">
          <span className="pf-num">{formatFileSize(item.image_meta?.bytes)}</span>
        </MetaRow>
        <div className="min-w-0 tablet:col-span-2">
          <dt className="text-meta text-muted">相对路径</dt>
          <dd className="min-w-0 break-all text-meta text-secondary">{item.file_path}</dd>
        </div>
      </dl>

      <RecognitionSection evidenceId={item.id} summary={item.recognition_summary} />

      <div className="rounded-control border border-line bg-surface p-3">
        {item.used_rows > 0 ? (
          <div className="space-y-1.5">
            <p className="text-body text-secondary">
              关联账本行{' '}
              <strong className="pf-num text-ink">{item.used_rows}</strong>{' '}
              条（全行口径，含被更正的 superseded 历史行）。
            </p>
            {/* 去向由 href 承载，不带 onClick：本组件在详情页与 Modal 两处复用，
                关闭动作属于宿主（Modal 随路由跳转卸载），不下沉进展示组件（票 07）。 */}
            <Link
              href={`/records?evidence_id=${item.id}&view=ledger`}
              className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control text-body-strong font-medium text-link"
            >
              在记录页查看这些行
              <ArrowRight size={15} aria-hidden />
            </Link>
            <p className="text-meta text-muted">
              跳转固定带 <code className="rounded bg-subtle px-1">view=ledger</code>（记录明细）。记录明细默认只列 active 行：这 N 条里若含被更正的历史行，还需在记录页打开「显示历史（含被更正）」才能全部看到。
            </p>
          </div>
        ) : (
          <p className="text-body text-secondary">
            未关联任何账本行（未使用）：可以删除，删除后原图与记录一起从库和磁盘消失。
          </p>
        )}
      </div>

      {/* 票 02：从凭证下钻到它写过的资产项，再进同一个操作历史抽屉 */}
      {item.used_rows > 0 && (
        <EvidenceAffectedItems evidenceId={item.id} onOpenItemHistory={onOpenItemHistory} />
      )}
    </div>
  )
}