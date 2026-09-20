using Workerd = import "/workerd/workerd.capnp";
const config :Workerd.Config = (
  services = [
    (name = "startup-loop", worker = (
      compatibilityDate = "2026-09-16",
      modules = [(name = "bad-module.js", esModule = embed "bad-module.js")]
    ))
  ],
  sockets = [(name = "startup", address = "127.0.0.1:18873", http = (), service = "startup-loop")]
);
