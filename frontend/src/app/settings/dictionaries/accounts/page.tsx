import { redirect } from 'next/navigation'

/** 旧「账户」字典页已拆成机构与渠道（ADR-0019 D3）：保留路径做跳转，不保留旧语义。 */
export default function LegacyAccountsDictionaryPage() {
  redirect('/settings/dictionaries/institutions')
}
