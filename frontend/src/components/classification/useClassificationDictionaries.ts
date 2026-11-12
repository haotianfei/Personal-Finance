'use client'

import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

/**
 * 分类弹窗要用的六张字典（类别树 / 流动性 / 机构 / 渠道 / 成员 / 标签）。
 *
 * 缓存键与各页面完全一致（`['assetClasses']` 等），所以记录页打开弹窗时
 * 基本都是命中缓存、零请求；当前值表第一次打开才会顺手取一次。
 */
export function useClassificationDictionaries(enabled = true) {
  const classesQuery = useQuery({
    queryKey: ['assetClasses'],
    queryFn: api.getAssetClasses,
    enabled,
  })
  const levelsQuery = useQuery({
    queryKey: ['liquidityLevels'],
    queryFn: api.getLiquidityLevels,
    staleTime: Infinity,
    enabled,
  })
  const institutionsQuery = useQuery({
    queryKey: ['institutions'],
    queryFn: api.getInstitutions,
    enabled,
  })
  const channelsQuery = useQuery({
    queryKey: ['channels'],
    queryFn: api.getChannels,
    enabled,
  })
  const membersQuery = useQuery({
    queryKey: ['members'],
    queryFn: api.getMembers,
    enabled,
  })
  const tagsQuery = useQuery({
    queryKey: ['tags'],
    queryFn: api.getTags,
    enabled,
  })

  return {
    classesQuery,
    levelsQuery,
    institutionsQuery,
    channelsQuery,
    membersQuery,
    tagsQuery,
  }
}