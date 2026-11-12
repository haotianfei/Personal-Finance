#!/usr/bin/env node
/**
 * tokens.json → tokens.generated.css
 *
 * 单一来源是 tokens.json；这个脚本只做机械展开，不做设计决策。
 * 生成文件需要与 JSON 一起提交，`npm run tokens:check` 用来发现漂移。
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const source = path.join(root, 'src/styles/tokens.json')
const target = path.join(root, 'src/styles/tokens.generated.css')
const checkOnly = process.argv.includes('--check')

const tokens = JSON.parse(fs.readFileSync(source, 'utf8'))

function kebab(key) {
  return key
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/_/g, '-')
    .toLowerCase()
}

function cssValue(pathParts, value) {
  if (typeof value === 'number') {
    const joined = pathParts.join('.')
    if (/^(space|radius|control|shell|native|focus|table)\./.test(joined)) return `${value}px`
    if (/^type\..+\.(size|line)$/.test(joined)) return `${value}px`
    if (/^motion\.(fast|base|sheet)$/.test(joined)) return `${value}ms`
    return String(value)
  }
  return String(value)
}

function flatten(node, prefix, out) {
  for (const [key, value] of Object.entries(node)) {
    if (key.startsWith('$')) continue
    const next = [...prefix, kebab(key)]
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      flatten(value, next, out)
    } else if (value !== null && value !== undefined) {
      out.push([`--pf-${next.join('-')}`, cssValue(next, value)])
    }
  }
}

const vars = []
flatten(tokens, [], vars)

const body = vars.map(([name, value]) => `  ${name}: ${value};`).join('\n')
const css = `/* AUTO-GENERATED from src/styles/tokens.json by scripts/build-tokens.mjs — do not edit. */\n:root {\n${body}\n}\n`

if (checkOnly) {
  const current = fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : ''
  if (current !== css) {
    console.error('tokens.generated.css is out of date. Run: npm run tokens:build')
    process.exit(1)
  }
  console.log(`tokens.generated.css is in sync (${vars.length} variables)`)
} else {
  fs.writeFileSync(target, css)
  console.log(`wrote ${path.relative(root, target)} (${vars.length} variables)`)
}