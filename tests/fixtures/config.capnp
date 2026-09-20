using Workerd = import "/workerd/workerd.capnp";
const config :Workerd.Config = (
  services = [
    (name = "a", worker = (
      compatibilityDate = "2026-09-16",
      modules = [
        (name = "worker-a.js", esModule = embed "worker-a.js"),
        (name = "bad-module.js", esModule = embed "bad-module.js")
      ],
      bindings = [(name = "B", service = "b")]
    )),
    (name = "b", worker = (
      compatibilityDate = "2026-09-16",
      modules = [(name = "worker-b.js", esModule = embed "worker-b.js")]
    ))
  ],
  sockets = [
    (name = "a", address = "127.0.0.1:18871", http = (), service = "a"),
    (name = "b", address = "127.0.0.1:18872", http = (), service = "b")
  ]
);
