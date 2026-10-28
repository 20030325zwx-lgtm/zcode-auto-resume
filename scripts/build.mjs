import { build } from 'esbuild';
import { readFile, writeFile, mkdir, readdir, copyFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';

await mkdir('dist', { recursive:true });
const template = await readFile('ui/panel.html', 'utf8');
const inputs = new Set();
for (const [native, output] of [[false, 'dashboard.html'], [true, 'dist/panel.html']]) {
  const bundle = await build({ entryPoints:['ui/main.js'], bundle:true, write:false, format:'esm', target:'es2022', minify:true, metafile:true, define:{__MCP_APP__:String(native)} });
  Object.keys(bundle.metafile.inputs).forEach((input) => inputs.add(input));
  const script = bundle.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
  await writeFile(output, template.replace('/* APP_SCRIPT */', () => script));
}
const server = await build({ entryPoints:['src/server.mjs'], bundle:true, platform:'node', format:'esm', target:'node20', outfile:'dist/server.mjs', minify:true, metafile:true, banner:{js:'import { createRequire as __createRequire } from "node:module"; const require = __createRequire(import.meta.url);'} });
Object.keys(server.metafile.inputs).forEach((input) => inputs.add(input));

const packages = new Map();
for (const input of inputs) {
  if (!input.includes('node_modules/')) continue;
  let dir = dirname(resolve(input));
  while (dir.includes('node_modules')) {
    try {
      const pkg = JSON.parse(await readFile(join(dir,'package.json'), 'utf8'));
      if (pkg.name && pkg.version) { packages.set(dir,pkg); break; }
    } catch { /* walk to package root */ }
    dir = dirname(dir);
  }
}
const notices = [];
for (const [dir, pkg] of [...packages].sort(([a],[b]) => a.localeCompare(b))) {
  const licenses = (await readdir(dir)).filter((name) => /^(license|licence|copying|notice)(\.|$)/i.test(name));
  const texts = await Promise.all(licenses.map((name) => readFile(join(dir,name),'utf8')));
  notices.push(`${pkg.name} ${pkg.version}\nLicense: ${pkg.license || 'See package'}\n${texts.join('\n')}`);
}
await writeFile('dist/THIRD_PARTY_NOTICES.txt', notices.join('\n\n---\n\n'));
// Only this directory is installed by the marketplace; no node_modules or tests.
for (const name of ['.zcode-plugin/plugin.json', 'hooks/hooks.json', 'monitor.py', 'desktop_send.applescript', 'dashboard.html', 'README.md', 'dist/server.mjs', 'dist/panel.html', 'dist/THIRD_PARTY_NOTICES.txt']) {
  const target = join('plugin', name);
  await mkdir(dirname(target), {recursive:true});
  await copyFile(name, target);
}
console.log('Built web dashboard, ZCode panel and bundled MCP server.');
