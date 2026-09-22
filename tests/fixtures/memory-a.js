import {WorkerEntrypoint} from 'cloudflare:workers';

export class Rpc extends WorkerEntrypoint {
  hello() { return 'rpc-ok'; }
}

const retained = [];
let instanceId;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export default {
  async fetch(request, env, ctx) {
    instanceId ??= crypto.randomUUID();
    const path = new URL(request.url).pathname;
    if (path === '/state') return Response.json({id: instanceId, retained: retained.length});
    if (path === '/soft' || path === '/retire-spin' || path === '/gc-spin') {
      retained.push(new Uint8Array(68 * 1024 * 1024).fill(1));
      if (path === '/gc-spin') {
        const ring = new Array(4096);
        for (let i = 0; i < 1000000; i++) ring[i % ring.length] = {value: i};
        for (;;) {}
      }
      if (path === '/retire-spin') {
        await sleep(1);
        for (;;) {}
      }
      return new Response(instanceId);
    }
    if (path === '/slow') {
      await env.B.fetch('http://b/tick');
      await sleep(250);
      return new Response(instanceId);
    }
    if (path === '/background-once') {
      ctx.waitUntil((async () => {
        await sleep(250);
        await env.B.fetch('http://b/tick');
      })());
    }
    if (path === '/heap') {
      for (;;) retained.push(new Array(16384).fill(retained.length));
    }
    if (path === '/native-iterator') {
      new Headers({
        [Symbol.iterator]() { return this; },
        next() {
          retained.push(new Array(16384).fill(retained.length));
          return {done: false, value: ['x-test', 'value']};
        }
      });
    }
    if (path === '/buffers') {
      for (;;) retained.push(new Uint8Array(4 * 1024 * 1024).fill(1));
    }
    if (path === '/large-buffer') {
      retained.push(new Uint8Array(96 * 1024 * 1024).fill(1));
    }
    if (path === '/retain') {
      retained.push(new Uint8Array(4 * 1024 * 1024).fill(1));
    }
    if (path === '/pending') await sleep(60000);
    if (path === '/external') return env.UPSTREAM.fetch('http://upstream/slow');
    if (path === '/ws') {
      const [client, server] = Object.values(new WebSocketPair());
      server.accept();
      server.addEventListener('message', event => server.send(event.data));
      return new Response(null, {status: 101, webSocket: client});
    }
    if (path === '/arm') {
      ctx.waitUntil((async () => {
        for (;;) {
          await env.B.fetch('http://b/tick');
          await sleep(25);
        }
      })());
    }
    if (path === '/stream' || path === '/short-stream') {
      return new Response(new ReadableStream({
        async start(controller) {
          controller.enqueue(new TextEncoder().encode('started\n'));
          await sleep(path === '/short-stream' ? 250 : 60000);
          controller.enqueue(new TextEncoder().encode('finished\n'));
          controller.close();
        }
      }));
    }
    return new Response('a-ok');
  }
};
