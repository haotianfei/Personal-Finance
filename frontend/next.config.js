/** @type {import('next').NextConfig} */
const nextConfig = {
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
  // 允许开发模式下的跨域请求
  allowedDevOrigins: ['127.0.0.1', 'localhost', '*'],
  // 禁用图片优化，避免在 Docker 中出现问题
  images: {
    unoptimized: true,
  },
}

module.exports = nextConfig
