/**
 * 宿主能力探测：只用于能力降级，不用于视觉分叉（design §2.4/§2.5）。
 * WebView/小程序内 Blob + <a download> 不保证生效，导出页据此做保守降级。
 */
export function isDownloadRestrictedWebView(): boolean {
  if (typeof navigator === 'undefined') return false
  const ua = navigator.userAgent || ''
  return (
    /miniProgram/i.test(ua) ||
    /MicroMessenger/i.test(ua) ||
    /AlipayClient/i.test(ua) ||
    /DingTalk/i.test(ua) ||
    (/Android/i.test(ua) && /;\s*wv\)/i.test(ua))
  )
}
