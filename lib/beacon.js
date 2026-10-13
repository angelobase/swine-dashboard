'use strict';
// UDP "hub beacon": every couple of seconds the dashboard announces "the MQTT broker is here" on the local network,
// so a prototype that is set to mqtt.host = "auto" can find it without a typed IP address. No packages needed.
// The beacon carries no secrets; a Pi uses the packet's source address as the hub address.
const dgram = require('node:dgram');
const os = require('node:os');

// Directed broadcast address of every active IPv4 network (e.g. 192.168.1.255), plus loopback for same-machine use.
function broadcastTargets() {
  const out = new Set(['127.0.0.1']);
  for (const list of Object.values(os.networkInterfaces())) {
    for (const i of list || []) {
      if (i.family !== 'IPv4' || i.internal || !i.netmask) continue;
      const a = i.address.split('.').map(Number), m = i.netmask.split('.').map(Number);
      out.add(a.map((x, k) => (x & m[k]) | ((~m[k]) & 255)).join('.'));
    }
  }
  return [...out];
}

function startBeacon({ port, mqttPort, requireAuth, intervalMs = 2000, log = console }) {
  const sock = dgram.createSocket('udp4');
  let timer = null;
  sock.on('error', (e) => log.error('[beacon] ' + e.message));
  sock.bind(0, () => {
    try { sock.setBroadcast(true); } catch (e) { log.error('[beacon] ' + e.message); return; }
    const msg = Buffer.from(JSON.stringify({ swine: 1, mqtt_port: mqttPort, auth: !!requireAuth, name: os.hostname().slice(0, 40) }));
    const tick = () => { for (const ip of broadcastTargets()) sock.send(msg, port, ip, () => {}); }; // unreachable networks: ignored
    tick(); timer = setInterval(tick, intervalMs); timer.unref();
  });
  return { close: () => { if (timer) clearInterval(timer); try { sock.close(); } catch {} } };
}
module.exports = { startBeacon, broadcastTargets };
