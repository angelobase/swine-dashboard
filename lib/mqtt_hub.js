'use strict';
// Embedded MQTT broker (aedes). Replaces the separate Mosquitto install AND tools/mqtt_bridge.py:
// the dashboard process now IS the broker, and handles each prototype's messages in-process.
//
// Topics:
//   swine/<device>/reading | state | event | status | preview/rgb | preview/thermal   (Pi -> dashboard)
//   swine/<device>/session (retained) | sample                                           (Pi -> dashboard, scheduled sessions)
//   swine/<device>/cmd                                                                   (dashboard -> Pi)
const net = require('node:net');

const DEVICE_RE = /^[A-Za-z0-9_-]{1,40}$/;

// 'mqtt://host:1883/proto-3f9a2c', 'host:1883/proto-3f9a2c', 'host/proto-3f9a2c' or just 'proto-3f9a2c' -> device id | null.
// The host part is ignored: the broker is always this process.
function deviceFromAddress(text) {
  if (typeof text !== 'string') return null;
  const last = text.trim().replace(/^[a-z]+:\/\//i, '').split('/').filter(Boolean).pop();
  return last && DEVICE_RE.test(last) ? last : null;
}

async function createHub(opts) {
  const { mqtt: m, resolve, onReading, onState, onEvent, onPreview, onSession, onSample, onUnknown, httpServer, log = console } = opts;
  let Aedes;
  try { ({ Aedes } = require('aedes')); }
  catch { throw new Error("the 'aedes' package is not installed. Run:  npm install aedes@1.2.0"); }
  const aedes = await Aedes.createBroker({ maxClientsIdLength: 64 });
  const requireAuth = !!m.password;

  aedes.authenticate = (client, username, password, cb) => {
    if (!requireAuth) return cb(null, true);
    const ok = username === m.username && password && password.toString() === m.password;
    if (ok) return cb(null, true);
    const err = new Error('Bad MQTT credentials'); err.returnCode = 4;
    return cb(err, false);
  };
  // A device may only publish under its own id, and may only listen to its own command topic.
  // This stops one Pi (or anything else on the Wi-Fi) from posting data as another pen.
  aedes.authorizePublish = (client, packet, cb) => {
    if (packet.topic.startsWith(`swine/${client.id}/`) && !packet.topic.endsWith('/cmd')) return cb(null);
    return cb(new Error('publish not allowed on ' + packet.topic));
  };
  aedes.authorizeSubscribe = (client, sub, cb) => cb(null, sub.topic === `swine/${client.id}/cmd` ? sub : null);

  aedes.on('publish', (packet, client) => {
    const t = /^swine\/([A-Za-z0-9_-]{1,40})\/(.+)$/.exec(packet.topic);
    if (!t) return;
    const [, dev, suffix] = t;
    const pen = resolve(dev);
    if (!pen) { // not added as a pen yet: its data is ignored, but the dashboard offers it in the Add-pen list
      if (onUnknown && client) onUnknown(dev, String((client.conn && client.conn.remoteAddress) || '').replace(/^::ffff:/, ''));
      return;
    }
    try {
      if (suffix === 'preview/rgb' || suffix === 'preview/thermal') return onPreview(pen, suffix.split('/')[1], packet.payload);
      if (suffix === 'status') return onState(pen, { online: packet.payload.toString() === 'online' });
      const body = JSON.parse(packet.payload.toString('utf8'));
      if (!body || typeof body !== 'object') return;
      if (suffix === 'reading') onReading(pen, body);
      else if (suffix === 'state') onState(pen, body);
      else if (suffix === 'event') onEvent(pen, body);
      else if (suffix === 'session' && onSession) onSession(pen, body);
      else if (suffix === 'sample' && onSample) onSample(pen, body);
    } catch (e) {
      log.error(`[mqtt] ${dev}/${suffix}: ${e.message}`);
    }
  });
  aedes.on('clientError', (c, e) => log.error(`[mqtt] client ${c && c.id}: ${e.message}`));
  aedes.on('connectionError', (_c, e) => log.error(`[mqtt] connection: ${e.message}`));

  // Plain MQTT on its own TCP port (local network).
  let server = null;
  if (m.tcp !== false) {
    server = net.createServer(aedes.handle);
    await new Promise((ok, bad) => { server.once('error', bad); server.listen(m.port, m.host, ok); });
  }
  // MQTT over WebSockets on the dashboard's web port, path m.websocket_path (e.g. wss://<app>.onrender.com/mqtt).
  // Hosting such as Render only forwards the web port. Only enabled with a password, because it is reachable from the internet.
  let wss = null, wsNote = 'off';
  if (httpServer && m.websocket_path) {
    let WS = null;
    try { WS = require('ws'); } catch { wsNote = "off: the 'ws' package is not installed (npm install ws@8)"; }
    if (WS && !requireAuth) wsNote = 'off: set an MQTT password (mqtt.password or MQTT_PASS) to enable it';
    else if (WS) {
      wss = new WS.WebSocketServer({ noServer: true, maxPayload: 2 * 1024 * 1024,
        handleProtocols: (protocols) => (protocols.has('mqtt') ? 'mqtt' : protocols.has('mqttv3.1') ? 'mqttv3.1' : false) });
      httpServer.on('upgrade', (req, socket, head) => {
        let p = '';
        try { p = new URL(req.url, 'http://x').pathname; } catch {}
        if (p !== m.websocket_path) { socket.destroy(); return; }
        wss.handleUpgrade(req, socket, head, (ws) => {
          const stream = WS.createWebSocketStream(ws);
          const fwd = String(req.headers['x-forwarded-for'] || '').split(',').map((x) => x.trim()).filter(Boolean).pop();
          stream.remoteAddress = fwd || req.socket.remoteAddress; // shown in the Add-pen list
          stream.on('error', () => {}); // a dropped connection is handled by aedes
          aedes.handle(stream, req);
        });
      });
      wsNote = `on at path ${m.websocket_path}`;
    }
  }

  return {
    requireAuth, websocket: wsNote, tcp: !!server,
    sendCommand(device, cmd) {
      return new Promise((ok) => aedes.publish({ topic: `swine/${device}/cmd`, payload: Buffer.from(JSON.stringify(cmd)), qos: 1, retain: false, cmd: 'publish', dup: false }, () => ok()));
    },
    connectedDevices: () => Object.keys(aedes.clients),
    close: () => new Promise((ok) => { if (server) server.close(); if (wss) wss.close(); aedes.close(() => ok()); }),
  };
}

module.exports = { createHub, deviceFromAddress };
