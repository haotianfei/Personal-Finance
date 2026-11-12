'use client'

import { FileText, Download } from 'lucide-react'
import { Card } from '@/components/ui/Card'

interface FormatGuideProps {
  showDownloadButton?: boolean
  onDownload?: () => void
}

// CSV 模板内容（ADR-0019 列口径：分类进 CSV 是「补空」，不覆盖项上已有分类）。
// 示例数据全部为虚构，不含任何真实机构/账户/金额。
export const CSV_TEMPLATE = `asset_date,asset_name,amount,side,asset_class,liquidity_days,institution,channel,owner,risk_level,tags
2026-01-31,示例活期账户,12345.67,asset,B 固定收益/B1 货币基金,0,示例银行,示例钱包,本人,低,应急
2026-01-31,示例定期存款,50000.00,asset,B 固定收益/B2 定期存款,365,示例银行,示例网点,配偶,低,
2026-01-31,示例指数基金,20000.00,asset,C 权益/C1 宽基指数,7,示例基金公司,示例平台,本人,中,长期配置
2026-01-31,示例信用卡欠款,3000.00,liability,F 负债/F1 消费信贷,30,示例银行,示例信用卡,本人,,
`

// 下载模板函数
export function downloadTemplate() {
  const blob = new Blob([CSV_TEMPLATE], { type: 'text/csv;charset=utf-8;' })
  const url = window.URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'assets_template.csv'
  document.body.appendChild(a)
  a.click()
  window.URL.revokeObjectURL(url)
  document.body.removeChild(a)
}

// 字段定义（11 列：3 列账本事实 + 必填分类 + 可选风险/标签）
export const CSV_FIELDS = [
  { key: 'asset_date', name: '日期', example: 'YYYY-MM-DD / YYYY/MM/DD' },
  { key: 'asset_name', name: '资产名称', example: '如: 示例活期账户' },
  { key: 'amount', name: '金额', example: '如: 12345.67' },
  { key: 'side', name: '资产/负债', example: 'asset / liability（缺省 asset）' },
  { key: 'asset_class', name: '资产类别', example: '全路径，如: B 固定收益/B1 货币基金' },
  { key: 'liquidity_days', name: '流动性(天)', example: '档位天数，如: 0 / 7 / 365' },
  { key: 'institution', name: '机构', example: '如: 示例银行（须已在字典中）' },
  { key: 'channel', name: '渠道', example: '如: 示例钱包（可选）' },
  { key: 'owner', name: '归属成员', example: '如: 本人（members 名称）' },
  { key: 'risk_level', name: '风险等级', example: '可选，如: 低/中/高' },
  { key: 'tags', name: '标签', example: '可选，多个用 | 分隔' },
]

export function FormatGuide({ showDownloadButton = false, onDownload }: FormatGuideProps) {
  return (
    <Card padding="md">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <FileText size={18} className="text-secondary" />
          <h2 className="text-h3 font-semibold text-ink">CSV 格式说明</h2>
        </div>
        {showDownloadButton && onDownload && (
          <button
            type="button"
            onClick={onDownload}
            className="inline-flex min-h-[44px] items-center gap-2 rounded-control border border-line-strong bg-surface px-3 text-body text-ink"
          >
            <Download size={16} aria-hidden />
            下载模板
          </button>
        )}
      </div>
      <p className="mt-2 text-body text-secondary">
        CSV 文件必须包含以下 11 个字段（列顺序不限；资产类别、流动性、机构、归属为必填分类）：
      </p>
      <div className="mt-4 grid grid-cols-2 gap-2 text-body tablet:grid-cols-4 xl:grid-cols-7">
        {CSV_FIELDS.map((field) => (
          <div key={field.key} className="rounded-control border border-line bg-subtle px-3 py-2">
            <span className="text-meta text-muted">{field.key}</span>
            <p className="font-medium text-ink">{field.name}</p>
            <p className="text-meta text-muted">{field.example}</p>
          </div>
        ))}
      </div>
      <div className="mt-4 rounded-control border border-line bg-brand-50 p-3 text-body text-brand-800">
        <p className="font-medium">使用说明</p>
        <ul className="mt-1 list-disc space-y-1 pl-5">
          <li>CSV 文件必须使用 UTF-8 编码，否则中文可能显示乱码</li>
          <li>日期格式支持：YYYY-MM-DD（如 2026-01-31）或 YYYY/MM/DD（如 2026/01/31）</li>
          <li>金额支持小数，不需要添加千分位分隔符</li>
          <li>资产类别/机构/渠道/归属/标签只在资产项上「补空」，不会覆盖已有值；字典项请在「设置 → 字典」中先建好</li>
          {showDownloadButton && <li>点击右上角「下载模板」可获取示例 CSV 文件</li>}
        </ul>
      </div>
    </Card>
  )
}