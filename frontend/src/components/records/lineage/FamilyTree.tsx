'use client'

import { ArrowDown, ArrowLeft, ArrowRight, ChevronRight } from 'lucide-react'
import { formatDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { LineageFamilyMember } from '@/types'
import { ATTACH_LINK_TYPE_LABEL } from './shared'

/**
 * 身份族三栏：前身 ← 本项 → 后继（`GET /lineage-links/family`，spec §1.2）。
 *
 * - 成员显示链类型、生效日、状态；点击成员把弹层切到该资产项（父级按 item_id 换行）；
 * - 移动端没有横向空间，三栏改为「前身 ↓ 本项 ↓ 后继」的纵排，箭头随断点转向；
 * - 只陈述关系，不在这里做金额合并（族合计在分析页）。
 */
export interface FamilyTreeProps {
  members: readonly LineageFamilyMember[]
  /** 本项 id（family 响应含自身；缺失时用传入的名称/状态兜底）。 */
  selfId: number
  selfName: string
  selfStatus: 'active' | 'archived'
  /** 切换弹层到族内成员；缺省时成员为纯展示。 */
  onSelect?: (itemId: number) => void
}

function sortMembers(members: LineageFamilyMember[]): LineageFamilyMember[] {
  return [...members].sort((left, right) => {
    const leftDate = left.effective_from ?? ''
    const rightDate = right.effective_from ?? ''
    if (leftDate !== rightDate) return leftDate.localeCompare(rightDate)
    return left.item_id - right.item_id
  })
}

function StatusPill({ status }: { status: string }) {
  const archived = status === 'archived'
  return (
    <span
      className={cn(
        'rounded-pill px-1.5 py-px text-micro',
        archived
          ? 'bg-status-warn-surface text-status-warn-text'
          : 'bg-subtle text-secondary',
      )}
    >
      {archived ? '已归档' : '在用'}
    </span>
  )
}

function MemberCard({
  member,
  onSelect,
}: {
  member: LineageFamilyMember
  onSelect?: (itemId: number) => void
}) {
  const label = member.link_type ? ATTACH_LINK_TYPE_LABEL[member.link_type] ?? member.link_type : null
  const content = (
    <>
      <span className="block w-full break-words text-meta font-medium text-ink">
        {member.name}
      </span>
      <span className="mt-0.5 flex w-full flex-wrap items-center gap-x-1.5 gap-y-0.5 text-micro text-muted">
        {label && (
          <span className="rounded-pill border border-line bg-subtle px-1.5 py-px text-micro">
            {label}
          </span>
        )}
        {member.effective_from && (
          <span className="pf-num">{formatDate(member.effective_from)} 起</span>
        )}
        <StatusPill status={member.status} />
        {onSelect && (
          <ChevronRight
            size={13}
            className="ml-auto shrink-0 transition-transform group-hover:translate-x-0.5 group-hover:text-brand-600 motion-reduce:transition-none motion-reduce:group-hover:translate-x-0"
            aria-hidden
          />
        )}
      </span>
    </>
  )

  const base = 'flex min-h-[44px] w-full flex-col items-start rounded-control border border-line bg-surface px-2.5 py-2 text-left'

  if (!onSelect) {
    return <div className={base}>{content}</div>
  }

  return (
    <button
      type="button"
      onClick={() => onSelect(member.item_id)}
      title={`切换到「${member.name}」`}
      className={cn(
        base,
        'group transition-colors hover:border-line-strong hover:bg-subtle',
        'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--pf-color-focus)]',
      )}
    >
      {content}
    </button>
  )
}

function FamilyColumn({
  title,
  members,
  emptyText,
  onSelect,
}: {
  title: string
  members: LineageFamilyMember[]
  emptyText: string
  onSelect?: (itemId: number) => void
}) {
  return (
    <div className="min-w-0 space-y-1 tablet:flex-1">
      <p className="text-micro font-medium text-muted">
        {title}
        {members.length > 0 && <span className="pf-num"> · {members.length}</span>}
      </p>
      {members.length === 0 ? (
        <p className="rounded-control border border-dashed border-line px-3 py-2 text-micro text-muted">
          {emptyText}
        </p>
      ) : (
        members.map((member) => (
          <MemberCard key={member.item_id} member={member} onSelect={onSelect} />
        ))
      )}
    </div>
  )
}

function Connector({ side }: { side: 'left' | 'right' }) {
  const Icon = side === 'left' ? ArrowLeft : ArrowRight
  return (
    <div className="flex shrink-0 items-center justify-center text-muted tablet:w-5" aria-hidden>
      <ArrowDown size={14} className="tablet:hidden" />
      <Icon size={14} className="hidden tablet:block" />
    </div>
  )
}

export function FamilyTree({ members, selfId, selfName, selfStatus, onSelect }: FamilyTreeProps) {
  const predecessorList = sortMembers(members.filter((member) => member.relationship === 'predecessor'))
  const successorList = sortMembers(members.filter((member) => member.relationship === 'successor'))
  const selfMember = members.find((member) => member.item_id === selfId)
  const selfLabel = selfMember?.name || selfName

  return (
    <div className="space-y-2">
      <div className="flex flex-col gap-2 tablet:flex-row tablet:items-stretch">
        <FamilyColumn
          title="前身"
          members={predecessorList}
          emptyText="无前身"
          onSelect={onSelect}
        />
        <Connector side="left" />
        <div className="rounded-control border border-brand-200 bg-brand-50/40 px-2.5 py-2 tablet:flex-1">
          <p className="text-micro font-medium text-brand-700">本项</p>
          <p className="break-words text-meta font-semibold text-ink">{selfLabel}</p>
          <span className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-micro text-muted">
            <StatusPill status={selfMember?.status ?? selfStatus} />
            <span>当前查看</span>
            {selfMember?.effective_from && (
              <span className="pf-num">{formatDate(selfMember.effective_from)} 起终止计量</span>
            )}
          </span>
        </div>
        <Connector side="right" />
        <FamilyColumn
          title="后继"
          members={successorList}
          emptyText="无后继"
          onSelect={onSelect}
        />
      </div>
      <p className="text-micro text-muted">
        {onSelect ? '点击族内成员可切换到该资产项。' : null}
        关联只改归属口径，历史记录仍留在原项。
      </p>
    </div>
  )
}