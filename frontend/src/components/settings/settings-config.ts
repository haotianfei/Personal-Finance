import type { LucideIcon } from 'lucide-react'
import {
  BellRing, DatabaseBackup, FileDown, FolderTree, Info, Plug,
  Smartphone, Tags, Users, Wallet, Gauge, Bot, Settings2, ScrollText,
} from 'lucide-react'

export interface SettingsItem {
  href: string
  label: string
  description: string
  icon: LucideIcon
}

export interface SettingsSection {
  id: string
  label: string
  description: string
  icon: LucideIcon
  items: SettingsItem[]
}

export const SETTINGS_SECTIONS: SettingsSection[] = [
  {
    id: 'data-backup',
    label: '数据与备份',
    description: '数据安全、恢复与审计。',
    icon: DatabaseBackup,
    items: [
      { href: '/settings/backup-restore', label: '备份与恢复', description: '备份、恢复与保留策略', icon: DatabaseBackup },
      { href: '/settings/audit', label: '操作日志', description: '全部操作记录与链状态', icon: ScrollText },
    ],
  },
  {
    id: 'dictionaries',
    label: '字典与属性',
    description: '资产类别、机构与渠道、流动性档位、成员与支出分类。',
    icon: Tags,
    items: [
      { href: '/settings/dictionaries/asset-classes', label: '资产类别', description: '类别树（叶子可挂资产项）', icon: FolderTree },
      { href: '/settings/dictionaries/institutions', label: '机构与渠道', description: '机构与渠道两个维度', icon: Wallet },
      { href: '/settings/dictionaries/liquidity-levels', label: '流动性档位', description: '固定 8 档（只读）', icon: Gauge },
      { href: '/settings/dictionaries/members', label: '成员', description: '归属成员与角色', icon: Users },
      { href: '/settings/dictionaries/tags', label: '标签', description: '横切标记（每项最多 5 个）', icon: Tags },
      { href: '/settings/dictionaries/expense-categories', label: '支出分类', description: '支出分类字典', icon: Tags },
    ],
  },
  {
    id: 'automation',
    label: '自动化与预警',
    description: '会替用户自动做事或触发提醒的规则。',
    icon: BellRing,
    items: [
      { href: '/settings/automation/alerts', label: '预警规则', description: '阈值、比较方式与通知', icon: BellRing },
      { href: '/settings/automation/alerts/import-export', label: '规则导入导出', description: '批量迁移预警规则', icon: FileDown },
    ],
  },
  {
    id: 'integrations',
    label: '采集与 AI',
    description: '截图来源、模型供应商等外部集成。',
    icon: Plug,
    items: [
      { href: '/settings/source-groups', label: '来源组', description: '截图来源与账户绑定', icon: Smartphone },
      { href: '/settings/llm-providers', label: 'LLM 供应商', description: '识别与解析使用的模型', icon: Bot },
    ],
  },
  {
    id: 'about',
    label: '关于',
    description: '版本、仓库、隐私与诊断。',
    icon: Info,
    items: [
      { href: '/settings/about', label: '关于与诊断', description: '版本、仓库与运行信息', icon: Info },
    ],
  },
]

export const DICTIONARY_ITEMS: SettingsItem[] = [
  { href: '/settings/dictionaries/asset-classes', label: '资产类别', description: '类别树（叶子可挂资产项）', icon: FolderTree },
  { href: '/settings/dictionaries/institutions', label: '机构与渠道', description: '机构与渠道两个维度', icon: Wallet },
  { href: '/settings/dictionaries/liquidity-levels', label: '流动性档位', description: '固定 8 档（只读）', icon: Gauge },
  { href: '/settings/dictionaries/members', label: '成员', description: '归属成员与角色', icon: Users },
  { href: '/settings/dictionaries/tags', label: '标签', description: '横切标记（每项最多 5 个）', icon: Tags },
  { href: '/settings/dictionaries/expense-categories', label: '支出分类', description: '支出分类字典', icon: Tags },
]

export const SETTINGS_ICON = Settings2