let ticks = 0;
export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path === '/tick') return new Response(String(++ticks));
    if (path === '/ticks') return new Response(String(ticks));
    if (path.startsWith('/binding/')) return env.A.fetch('http://a/' + path.slice(9));
    if (path === '/rpc' || path === '/rpc-pending') {
      try {
        return new Response(await (path === '/rpc' ? env.RPC.hello() : env.RPC.hold()));
      } catch {
        return new Response('rpc-rejected');
      }
    }
    return new Response('b-ok');
  }
};
