import { spawn } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const webRoot = resolve(root, 'apps/web')
const outputRoot = resolve(webRoot, '.output/public')
const artifacts = resolve(root, 'artifacts')
const port = '3100'
const server = spawn(process.execPath, ['.output/server/index.mjs'], {
  cwd: webRoot,
  env: { ...process.env, NITRO_HOST: '127.0.0.1', NITRO_PORT: port },
  stdio: 'ignore'
})

async function waitForServer() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/inbox`)
      if (response.ok) return response
    } catch {}
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 100))
  }
  throw new Error('Nuxt snapshot server did not become ready')
}

try {
  const response = await waitForServer()
  let html = await response.text()
  const stylesheetPaths = [...html.matchAll(/<link rel="stylesheet" href="([^"]+)"/g)].map((match) => match[1])
  const styles = await Promise.all(
    stylesheetPaths.map((href) => readFile(resolve(outputRoot, href.replace(/^\//, '')), 'utf8'))
  )
  const artwork = await readFile(resolve(webRoot, 'public/images/setcrawler-artworks.webp'))
  const artworkData = `data:image/webp;base64,${artwork.toString('base64')}`
  const css = styles
    .join('\n')
    .replaceAll(/url\((?:"|')?\/images\/setcrawler-artworks\.webp(?:"|')?\)/g, `url("${artworkData}")`)
  html = html
    .replaceAll(/<script[\s\S]*?<\/script>/g, '')
    .replaceAll(/<link rel="stylesheet"[^>]+>/g, '')
    .replace('</head>', `<style>${css}</style></head>`)
    .replaceAll(/url\((?:"|')?\/images\/setcrawler-artworks\.webp(?:"|')?\)/g, `url("${artworkData}")`)

  const mobileHtml = html
    .replaceAll(/@media\s*\(max-width:\s*(?:1240|980|760|430)px\)/g, '@media (max-width: 2000px)')
    .replace('</head>', '<style>html,body,.app-frame{width:390px!important;max-width:390px!important;margin:0!important;overflow-x:hidden!important}</style></head>')

  await mkdir(artifacts, { recursive: true })
  await writeFile(resolve(artifacts, 'inbox-desktop-inline.html'), html)
  await writeFile(resolve(artifacts, 'inbox-mobile-inline.html'), mobileHtml)
  console.log('Rendered inline desktop and mobile SSR snapshots.')
} finally {
  server.kill('SIGTERM')
}
