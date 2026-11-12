'use client'

import { createContext, useContext, useEffect, useRef } from 'react'

/**
 * 「确认」前的必填门禁：子内容里还缺东西时，确认这一步先别放行。
 *
 * 为什么单独放一个文件：`ConfirmDialog` 在部分页面是按需加载的（`dynamic(..., {ssr:false})`），
 * 若子控件为了拿门禁去 import 对话框本体，等于把懒加载拆掉。这里只放一个 context + 一个 hook，
 * 两边各取所需，谁也不用把对方拉进自己的包。
 */
export interface ConfirmGate {
  /** 现在能放行吗？每次点「确认」时求值——门禁只读不触发重渲染。 */
  blocked: () => boolean
  /** 被拦下时的反馈：把缺什么说出来，并把焦点送回缺的那一项。 */
  nudge: () => void
}

export interface ConfirmGateRegistry {
  register: (gate: ConfirmGate) => () => void
}

/**
 * 理由必填的判定（**唯一实现**）：空串或只有空白＝还没选。
 *
 * 三个地方都调这一条，不各写一份 `!reason`：确认框门禁、控件自己的红字提示、
 * 以及**自己画保存按钮**的弹窗（records 的更正框走 `ResponsiveDialog`，没有确认按钮）。
 */
export function isReasonMissing(reason: string | null | undefined): boolean {
  return (reason ?? '').trim() === ''
}

/**
 * 跑一次门禁：缺项就触发反馈并返回 true，调用方据此提前 return（不发请求、不关窗）。
 *
 * `ConfirmDialog` 的确认按钮与自画保存按钮的弹窗共用这一条；反馈长什么样由 `nudge` 决定，
 * 所以两种交互形态给出的是同一句话、同一个焦点落点。
 */
export function runConfirmGate(gate: ConfirmGate | null | undefined): boolean {
  if (!gate?.blocked()) return false
  gate.nudge()
  return true
}

/** 由 `ConfirmDialog` 提供；不在对话框里时为 null（门禁自动失效）。 */
export const ConfirmGateContext = createContext<ConfirmGateRegistry | null>(null)

/**
 * 把「我还缺一项」登记给外层确认框：`blocked` 为真时点确认不会执行 `onConfirm`。
 *
 * 不在 `ConfirmDialog` 里（如普通弹窗自己画提交按钮）时是 no-op ——
 * 那种入口得自己拦，或者靠后端 422 兜底。
 */
export function useConfirmGate(blocked: boolean, nudge: () => void): void {
  const registry = useContext(ConfirmGateContext)
  // 门禁每次渲染都可能变（理由一选就该放行）：用 ref 取最新值，登记只做一次。
  const latest = useRef({ blocked, nudge })

  useEffect(() => {
    latest.current = { blocked, nudge }
  }, [blocked, nudge])

  useEffect(() => {
    if (!registry) return
    return registry.register({
      blocked: () => latest.current.blocked,
      nudge: () => latest.current.nudge(),
    })
  }, [registry])
}