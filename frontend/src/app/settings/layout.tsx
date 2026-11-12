import { PageContainer } from '@/components/layout/PageContainer'
import { SettingsShell } from '@/components/settings/SettingsShell'

/**
 * 设置子树统一套页面容器：
 * - 宽度上限只由 `.pf-main` 一层承载，设置页不再自带 1200 盒宽；
 * - 内边距也只由容器给一次（根元素带 `data-pf-page`，不吃 `.pf-main` 的兜底内边距）。
 */
export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  return (
    <PageContainer width="wide">
      <SettingsShell>{children}</SettingsShell>
    </PageContainer>
  )
}
