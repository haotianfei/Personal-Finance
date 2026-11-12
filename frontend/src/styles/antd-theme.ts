import type { ThemeConfig } from 'antd'
import { color, control, focus, font, motion, radius, shadow, type } from './tokens'

/**
 * antd ConfigProvider 映射：从 tokens.json 派生，页面不直接改 antd 默认外观。
 *
 * 原则（design §3.4/§4.2）：
 * - 圆角压平为 8（控件）/ 12（卡片）/ 16（浮层），不再出现 6px 控件；
 * - 主色用 brand-600（白字 5.17:1），不再用 500 承载 12–14px 文字；
 * - 边框、表头、行 hover 与 Tailwind 语义色同源；
 * - 只有两级阴影：卡片 / 浮层。
 */
export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: color.action.primary,
    colorInfo: color.action.primary,
    colorLink: color.text.link,
    colorLinkHover: color.action['primary-hover'],
    colorSuccess: color.status.good,
    colorWarning: color.status.warn,
    colorError: color.status.critical,
    colorText: color.text.primary,
    colorTextSecondary: color.text.secondary,
    colorTextTertiary: color.text.muted,
    colorTextQuaternary: color.text.muted,
    colorTextDisabled: color.text.disabled,
    colorBgBase: color.bg.surface,
    colorBgLayout: color.bg.app,
    colorBgContainer: color.bg.surface,
    colorBgElevated: color.bg.surface,
    colorFillAlter: color.bg.subtle,
    colorBorder: color.border.default,
    colorBorderSecondary: color.border.default,
    colorSplit: color.border.default,
    borderRadius: radius.control,
    borderRadiusLG: radius.card,
    borderRadiusSM: radius.control,
    borderRadiusXS: radius.control,
    fontFamily: font.sans,
    fontSize: type.body.size,
    lineHeight: type.body.line / type.body.size,
    controlHeight: control['height-desktop'],
    controlHeightLG: control['height-touch'],
    controlHeightSM: control['height-desktop'],
    boxShadow: shadow.card,
    boxShadowSecondary: shadow.overlay,
    boxShadowTertiary: shadow.card,
    motionDurationFast: `${motion.fast}ms`,
    motionDurationMid: `${motion.base}ms`,
    wireframe: false,
  },
  components: {
    Button: {
      borderRadius: radius.control,
      borderRadiusLG: radius.control,
      borderRadiusSM: radius.control,
      controlHeight: control['height-desktop'],
      controlHeightLG: control['height-touch'],
      controlHeightSM: control['height-desktop'],
      fontWeight: 500,
      primaryShadow: 'none',
      defaultShadow: 'none',
      dangerShadow: 'none',
    },
    Input: {
      borderRadius: radius.control,
      controlHeight: control['height-desktop'],
      activeShadow: `0 0 0 ${focus.width}px ${color.action['primary-wash']}`,
      hoverBorderColor: color.border.strong,
    },
    InputNumber: {
      borderRadius: radius.control,
      controlHeight: control['height-desktop'],
    },
    Select: {
      borderRadius: radius.control,
      controlHeight: control['height-desktop'],
      optionSelectedBg: color.brand['50'],
    },
    DatePicker: {
      borderRadius: radius.control,
      controlHeight: control['height-desktop'],
    },
    Card: {
      borderRadiusLG: radius.card,
      paddingLG: 20,
    },
    Table: {
      headerBg: color.bg.subtle,
      headerColor: color.text.muted,
      headerSplitColor: color.border.default,
      borderColor: color.border.default,
      rowHoverBg: color.bg.subtle,
      cellPaddingBlock: 12,
      cellPaddingInline: 16,
      fontSize: type.body.size,
    },
    Modal: {
      borderRadiusLG: radius.overlay,
    },
    Drawer: {
      colorBgElevated: color.bg.surface,
    },
    Tooltip: {
      colorBgSpotlight: color.bg.inverse,
      borderRadius: radius.control,
    },
    Tabs: {
      itemSelectedColor: color.action.primary,
      inkBarColor: color.action.primary,
    },
    Pagination: {
      borderRadius: radius.control,
    },
    Message: {
      borderRadiusLG: radius.card,
    },
  },
}