/**
 * A minimal RFC 6455 server for the fake gateway.
 *
 * Node ships a WebSocket client and no server, and the adapter's real process
 * opens a real socket — so without this, an end-to-end run against the fake
 * would exercise the catch-up read and never the §3 stream. Only what that
 * stream needs is implemented: the handshake, masked text frames in, unmasked
 * text frames out, ping/pong, and close. No fragmentation, no binary, no
 * extensions, no permessage-deflate.
 *
 * Test-support code. Never loaded by the adapter itself.
 *
 * @module dsh_adapter/fake/wsserver
 */

import { createHash } from 'node:crypto'

/** The fixed GUID RFC 6455 mixes into the accept token. */
const HANDSHAKE_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

/** Opcodes this server understands. */
const OP = { text: 0x1, close: 0x8, ping: 0x9, pong: 0xa }

/**
 * The `Sec-WebSocket-Accept` value for one client key.
 *
 * @param {string} key - the client's `Sec-WebSocket-Key` header.
 * @returns {string} the base64 accept token.
 */
export function acceptToken(key) {
  return createHash('sha1').update(`${key}${HANDSHAKE_GUID}`).digest('base64')
}

/**
 * Encode one text frame for sending to a client (server frames are never masked).
 *
 * @param {string} text - the payload.
 * @returns {Buffer} the encoded frame.
 */
export function encodeTextFrame(text) {
  const payload = Buffer.from(text, 'utf8')
  const length = payload.length
  let header
  if (length < 126) {
    header = Buffer.from([0x80 | OP.text, length])
  } else if (length < 65_536) {
    header = Buffer.alloc(4)
    header[0] = 0x80 | OP.text
    header[1] = 126
    header.writeUInt16BE(length, 2)
  } else {
    header = Buffer.alloc(10)
    header[0] = 0x80 | OP.text
    header[1] = 127
    header.writeBigUInt64BE(BigInt(length), 2)
  }
  return Buffer.concat([header, payload])
}

/**
 * Pull as many complete frames as a buffer holds.
 *
 * @param {Buffer} buffer - accumulated bytes from the client.
 * @returns {{frames: Array<{opcode: number, text: string}>, rest: Buffer}} decoded frames and the unconsumed tail.
 */
export function decodeFrames(buffer) {
  const frames = []
  let offset = 0
  for (;;) {
    if (buffer.length - offset < 2) break
    const opcode = buffer[offset] & 0x0f
    const masked = (buffer[offset + 1] & 0x80) !== 0
    let length = buffer[offset + 1] & 0x7f
    let cursor = offset + 2
    if (length === 126) {
      if (buffer.length - cursor < 2) break
      length = buffer.readUInt16BE(cursor)
      cursor += 2
    } else if (length === 127) {
      if (buffer.length - cursor < 8) break
      length = Number(buffer.readBigUInt64BE(cursor))
      cursor += 8
    }
    let mask = null
    if (masked) {
      if (buffer.length - cursor < 4) break
      mask = buffer.subarray(cursor, cursor + 4)
      cursor += 4
    }
    if (buffer.length - cursor < length) break
    const payload = Buffer.from(buffer.subarray(cursor, cursor + length))
    if (mask) for (let i = 0; i < payload.length; i += 1) payload[i] ^= mask[i % 4]
    frames.push({ opcode, text: payload.toString('utf8') })
    offset = cursor + length
  }
  return { frames, rest: buffer.subarray(offset) }
}

/**
 * Attach a WebSocket upgrade handler to an HTTP server.
 *
 * @param {import('node:http').Server} server - the HTTP server to upgrade on.
 * @param {(connection: {send: (text: string) => void, close: () => void,
 *   onMessage: (handler: (text: string) => void) => void,
 *   onClose: (handler: () => void) => void, url: string}) => void} onConnection -
 *   called once per accepted socket.
 * @returns {void}
 */
export function attachWebSocketServer(server, onConnection) {
  server.on('upgrade', (request, socket) => {
    const key = request.headers['sec-websocket-key']
    if (typeof key !== 'string') {
      socket.destroy()
      return
    }
    socket.write([
      'HTTP/1.1 101 Switching Protocols',
      'Upgrade: websocket',
      'Connection: Upgrade',
      `Sec-WebSocket-Accept: ${acceptToken(key)}`,
      '\r\n',
    ].join('\r\n'))

    let buffer = Buffer.alloc(0)
    const messageHandlers = new Set()
    const closeHandlers = new Set()
    let open = true
    const finish = () => {
      if (!open) return
      open = false
      for (const handler of closeHandlers) handler()
    }

    const connection = {
      url: request.url ?? '',
      send(text) {
        if (open && !socket.destroyed) socket.write(encodeTextFrame(text))
      },
      close() {
        if (!open) return
        // 0x88 is a close frame with an empty payload.
        if (!socket.destroyed) socket.write(Buffer.from([0x88, 0x00]))
        finish()
        socket.end()
      },
      onMessage(handler) {
        messageHandlers.add(handler)
      },
      onClose(handler) {
        closeHandlers.add(handler)
      },
    }

    socket.on('data', chunk => {
      buffer = Buffer.concat([buffer, chunk])
      const { frames, rest } = decodeFrames(buffer)
      buffer = rest
      for (const frame of frames) {
        if (frame.opcode === OP.text) for (const handler of messageHandlers) handler(frame.text)
        else if (frame.opcode === OP.ping) socket.write(Buffer.from([0x80 | OP.pong, 0x00]))
        else if (frame.opcode === OP.close) connection.close()
      }
    })
    socket.on('close', finish)
    socket.on('error', finish)

    onConnection(connection)
  })
}
