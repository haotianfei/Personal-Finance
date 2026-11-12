/** @type {import('next').NextConfig} */
const { version } = require('./package.json')

const nextConfig = {
  env: { NEXT_PUBLIC_APP_VERSION: version },
  async rewrites() {
    // 在 Docker 环境中使用服务名，本地开发使用 localhost
    // 注意：这里使用 backend 服务名，因为前端和后端在同一个 Docker 网络中
    const backendUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

    console.log('Backend URL:', backendUrl)

    return [
      {
        source: '/api/:path*',
        destination: `${backendUrl}/api/:path*`,
      },
    ]
  },
  async redirects() {
    // IA v1.0：只列 7 条显式映射，permanent=true → 308；Next 默认保留 query。
    return [
      { source: '/accounts', destination: '/settings/dictionaries/accounts', permanent: true },
      { source: '/fund-types', destination: '/settings/dictionaries/fund-types', permanent: true },
      { source: '/liquidity-ratings', destination: '/settings/dictionaries/liquidity-ratings', permanent: true },
      { source: '/attributes', destination: '/settings/dictionaries/attributes', permanent: true },
      { source: '/asset-owners', destination: '/settings/dictionaries/owners', permanent: true },
      { source: '/expense-categories', destination: '/settings/dictionaries/expense-categories', permanent: true },
      { source: '/alerts/import-export', destination: '/settings/automation/alerts/import-export', permanent: true },
    ]
  },
  // 允许开发模式下的跨域请求
  allowedDevOrigins: ['127.0.0.1', 'localhost', '*'],
  // 禁用图片优化，避免在 Docker 中出现问题
  images: {
    unoptimized: true,
  },
  // 禁用 ESLint 检查，避免构建失败
  eslint: {
    ignoreDuringBuilds: true,
  },
  // 保持 TypeScript 类型检查，避免掩盖契约错误
  typescript: {
    ignoreBuildErrors: false,
  },
}

module.exports = nextConfig
