function burn(n) {
  let value = 123;
  for (let i = 0; i < n; ++i) value = (Math.imul(value, 1664525) + 1013904223) | 0;
  return value;
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const n = Number(url.searchParams.get('n') || 1000000);
    switch (url.pathname) {
      case '/loop': while (true) {}
      case '/microtasks': while (true) await Promise.resolve();
      case '/sleep': await sleep(250); return new Response('slept');
      case '/burn': return new Response(String(burn(n)));
      case '/chunks': {
        let value = 0;
        for (let i = 0; i < Number(url.searchParams.get('count') || 8); ++i) {
          value ^= burn(n);
          await sleep(20);
        }
        return new Response(String(value));
      }
      case '/binding': return env.B.fetch('http://b/health');
      case '/dynamic': await import('./bad-module.js'); return new Response('unexpected');
      default: return new Response('a-ok');
    }
  }
};
