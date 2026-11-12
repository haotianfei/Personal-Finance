import { redirect } from 'next/navigation'

/** 旧「所有者」字典已由成员（members）取代（ADR-0019 D6：家庭池退役）。 */
export default function LegacyOwnersDictionaryPage() {
  redirect('/settings/dictionaries/members')
}
