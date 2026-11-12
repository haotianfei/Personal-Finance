import { redirect } from 'next/navigation'

/** 旧「资产类型」字典页已由资产类别树取代（ADR-0019 D2）。 */
export default function LegacyFundTypesDictionaryPage() {
  redirect('/settings/dictionaries/asset-classes')
}
