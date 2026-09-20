export default {
  fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === '/burn') {
      let value = 123;
      const n = Number(url.searchParams.get('n') || 1000000);
      for (let i = 0; i < n; ++i) value = (Math.imul(value, 1664525) + 1013904223) | 0;
      return new Response(String(value));
    }
    return new Response('b-ok');
  }
};
