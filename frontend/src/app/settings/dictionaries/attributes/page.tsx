import { redirect } from 'next/navigation'

/** EAV 属性字典已整体下线（ADR-0019）：分类改由资产项 + 分类字典承载。 */
export default function LegacyAttributesDictionaryPage() {
  redirect('/settings/dictionaries/asset-classes')
}
