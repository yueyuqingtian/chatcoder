// plan-20261106-ta3-080：直接执行指定安装包的纯协议函数，生成不含真实账号的对照样本。
const asar = require('@electron/asar');
const crypto = require('node:crypto');
const archive = process.argv[2];
if (!archive) throw new Error('请指定 TA3 app.asar');
function load(file, dependencies = {}) {
  const source = asar.extractFile(archive, ('dist/main/' + file + '.js').replaceAll('/', require('path').sep)).toString();
  const names = [...source.matchAll(/^export function (\w+)/gm)].map(m => m[1]);
  for (const match of source.matchAll(/^export \{([^}]+)\};?/gm)) {
    names.push(...match[1].split(',').map(x => x.trim()).filter(Boolean));
  }
  const code = source.replace(/^import\s[\s\S]*?;\s*$/gm, '').replace(/^export \{[^}]+\};?/gm, '').replace(/^export /gm, '');
  return new Function(...Object.keys(dependencies), code + '\nreturn {' + names.join(',') + '};')(...Object.values(dependencies));
}
const snapshots = load('shared/skillLoadSnapshots');
const guard = load('chat/toolExchangeGuard');
const client = load('chat/modelClient', { LOCAL_ORG_ID: 'personal' });
// 样本仅含纯文本及内联工具结果；文件、附件和编辑器归一化端口不在此样本内执行。
const messages = load('chat/modelMessage', {
  normalizeStoredContextFiles: x => Array.isArray(x) ? x : [],
  normalizeStoredPrompts: x => Array.isArray(x) ? x : [],
  normalizeStoredSkillContexts: x => Array.isArray(x) ? x : [],
  normalizeArtifactRef: x => x, readToolArtifact: () => { throw new Error('样本不应读取工具产物'); },
  normalizeActiveDocument: x => x, ...guard, ...snapshots,
});
const openai = load('chat/openAiAdapter', { createHash: crypto.createHash, ...client, ...messages });
const anthropic = load('chat/anthropicAdapter', {
  ...client, ...snapshots, createProviderPayloadError: message => new Error(message),
});
const scenarios = {};
for (const provider of ['zai', 'deepseek', 'openai']) {
  const model = { model: 'fixture-model', provider, apiBase: 'https://models.example.test/v2', apiKey: 'llm-fixture',
    completionOptions: { thinkingEnabled: true, thinkingLevels: [{ level: 'low' }, { level: 'high' }], maxTokens: 4096 } };
  scenarios[provider] = {
    model, headers: client.buildModelHeaders(model, { catalog: { selectedOrgId: 'app-fixture' } }, undefined, 'ws-fixture'),
    stream: openai.buildRequestBody(model, [{ role: 'user', content: 'hello' }], [], { taskId: 'session-fixture' }),
    text: openai.buildTextRequestBody(model, [{ role: 'user', content: 'hello' }]),
  };
}
const model = { model: 'fixture-kimi', provider: 'kimi', apiBase: 'https://models.example.test/anthropic', apiKey: 'llm-fixture',
  completionOptions: { maxTokens: 4096 } };
scenarios.anthropic = { model, headers: client.buildAnthropicHeaders(model, { catalog: { selectedOrgId: 'app-fixture' } }, undefined, 'ws-fixture'),
  stream: anthropic.buildAnthropicRequestBody(model, [{ role: 'user', content: 'hello' }], [], {}) };
const replayInput = [
  { role: 'user', content: 'hello' },
  { role: 'assistant', content: 'progress' },
  { role: 'assistant', content: '', tool_calls: [{ id: 'tool-1', type: 'function', function: { name: 'Read', arguments: '{}' } }] },
  { role: 'tool', tool_call_id: 'tool-1', content: 'result' },
  { role: 'assistant', content: 'done' },
];
process.stdout.write(JSON.stringify({ version: JSON.parse(asar.extractFile(archive, 'package.json')).version,
  scenarios, reasoning: messages.ensureToolCallReasoningPassthrough(replayInput), cacheKey: openai.buildPromptCacheKey('session-fixture') }, null, 2));
