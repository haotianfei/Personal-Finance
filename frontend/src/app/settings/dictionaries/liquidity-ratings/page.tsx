import { redirect } from 'next/navigation'

/** 旧「流动性评级」CRUD 页已下线（ADR-0019 D4：8 档只读常量）。 */
export default function LegacyLiquidityRatingsDictionaryPage() {
  redirect('/settings/dictionaries/liquidity-levels')
}
