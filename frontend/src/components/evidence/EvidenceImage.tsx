'use client'

import { useState } from 'react'
import { ImageOff } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'

/**
 * 凭证原图：`<img loading="lazy">` 直出 + onError 置灰占位（spec §4 否决缩略图服务）。
 * 行在盘空（文件丢失）时不能落成破图图标，否则整张列表看起来像坏了。
 *
 * 列表缩略图与详情内容（record-provenance D8 的 `EvidenceDetail`）共用同一份实现：
 * 两处的失败占位必须长得一样，否则「原图不可用」在列表里是一块灰、在详情里是破图。
 */
export function EvidenceImage({ id, alt, className }: { id: number; alt: string; className?: string }) {
  const [failed, setFailed] = useState(false)
  if (failed) {
    return (
      <span
        className={cn(
          'flex items-center justify-center rounded-control border border-line bg-subtle text-muted',
          className,
        )}
        title="原图不可用（磁盘文件可能已丢失）"
      >
        <ImageOff size={18} aria-hidden />
        <span className="sr-only">原图不可用</span>
      </span>
    )
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element -- spec D3：原图直出 + loading=lazy，不引入缩略图服务/next-image 优化
    <img
      loading="lazy"
      src={api.evidenceImageUrl(id)}
      alt={alt}
      onError={() => setFailed(true)}
      className={cn('rounded-control border border-line bg-subtle object-contain', className)}
    />
  )
}