import type { NextConfig } from 'next'

const nextConfig: NextConfig = {
  async rewrites() {
    return {
      fallback: [
        {
          source:      '/api/:path*',
          destination: 'http://localhost:8000/api/:path*',
        },
      ],
    }
  },

  async headers() {
    return [
      {
        source: '/api/:path*',
        headers: [
          { key: 'Cache-Control',     value: 'no-store' },
          { key: 'X-Accel-Buffering', value: 'no'       },
        ],
      },
    ]
  },
}

export default nextConfig
