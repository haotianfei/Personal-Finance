'use client'

import Link from 'next/link'
import { useParams } from 'next/navigation'
import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '@/lib/api'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { ErrorState, LoadingState } from '@/components/ui/States'
import { EvidenceDetail } from '@/components/evidence/EvidenceDetail'

/**
 * 凭证详情页（record-provenance D6/D8）：`/data/evidence/<id>`。
 *
 * 凭证是实体，实体要有自己的地址——可硬刷新直达、可深链、后退正确，不必先经过
 * 列表页。数据只取一次单条详情（`GET /api/screenshots/evidence/{id}`，D7 让它是
 * 列表项的同源构造 + 一个 `file_sha256`），正文交给 `EvidenceDetail`：与列表 Modal
 * 共用同一份内容（D8），故本页**不**再渲染「关联账本行 / 下钻记录页」那一块
 * （票 07 已随组件搬入，页面上再放一个就是同屏两个入口）。
 *
 * 本页只读：无编辑 / 删除 / 重识别入口，也不改 `PageContainer` 的宽度策略
 * （沿用列表页的 `width="wide"`）。
 */

/** 返回证据库的入口（列表页路由）。404 与非法地址两个分支共用，文案只有一处。 */
function BackToEvidenceLibrary() {
  return (
    <Link href="/data/evidence" className="font-medium text-link underline">
      返回证据库
    </Link>
  )
}

export default function EvidenceDetailPage() {
  const params = useParams()
  // 路由参数是字符串：`Number('abc')` → NaN，`''` → 0。非整数 / 非正数（含小数、负数）
  // 都不是合法凭证 id，统一走「地址无效」分支——否则 `1.5` 会被发给后端换回 422，
  // 用户看到的是一句校验错误而不是「这个地址不对」。
  const rawId = Array.isArray(params.id) ? params.id[0] : params.id
  const id = Number(rawId)
  const validId = Number.isInteger(id) && id > 0

  const detailQuery = useQuery({
    queryKey: ['evidenceDetail', id],
    queryFn: () => api.getEvidence(id),
    enabled: validId,
    // 404 不重试：凭证不存在是确定性结论，重试只会把「凭证不存在」拖成几秒后的同一句话
    retry: false,
  })
  const item = detailQuery.data
  // 后端把 HTTPException(404) 转成统一信封（`code=NOT_FOUND`），故 `status` 是权威判据；
  // 文案「凭证不存在」随 `ApiError.message` 一起到，这里固定成页面标题，避免不同来源措辞漂移。
  const notFound = detailQuery.error instanceof ApiError && detailQuery.error.status === 404

  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader
        back={{ href: '/data/evidence', label: '返回证据库' }}
        title={validId ? `凭证 #${id}` : '凭证'}
      />

      {!validId ? (
        <ErrorState
          title="凭证地址无效"
          description={
            <>
              链接里的编号不是有效凭证号。<BackToEvidenceLibrary />
            </>
          }
        />
      ) : detailQuery.isLoading ? (
        <LoadingState rows={5} />
      ) : notFound ? (
        <ErrorState
          title="凭证不存在"
          description={
            <>
              这张凭证可能已被删除，或从未存在。<BackToEvidenceLibrary />
            </>
          }
        />
      ) : detailQuery.isError ? (
        <ErrorState
          title="凭证加载失败"
          description={detailQuery.error instanceof Error ? detailQuery.error.message : '请稍后重试。'}
          onRetry={() => { void detailQuery.refetch() }}
        />
      ) : item ? (
        <EvidenceDetail item={item} />
      ) : null}
    </PageContainer>
  )
}