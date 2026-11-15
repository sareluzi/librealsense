// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

import { defineConfig } from 'vite'

const backend = { target: 'http://localhost:8000', changeOrigin: true }

// Built into dist/, which the REST server serves at /samples/person-tracker/
export default defineConfig({
  base: '/samples/person-tracker/',
  server: { port: 3001, proxy: { '/api': backend, '/socket': { ...backend, ws: true } } },
})
