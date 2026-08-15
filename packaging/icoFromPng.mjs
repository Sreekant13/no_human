// Dependency-free PNG -> Windows .ico conversion.
//
// WHY THIS EXISTS. The Windows icon used to be derived once, by hand, from a
// committed .icns (packaging/make-win-icon.ps1) and then committed itself.
// The brand mark that master is cut from now compresses to a byte stream
// that coincidentally contains 3-byte scanner needle terms — a structural
// false positive the identity scanner cannot tell apart from a real leak, so
// neither derived binary is committed any more (see packaging/derive-icons.mjs).
// That means `.ico` generation has to run on every packaging build, on every
// platform (Windows CI has no `sips`/`iconutil`, and adding an image library
// just to resize a PNG is the kind of dependency this repo's build tooling
// deliberately avoids — see OUT OF SCOPE in the task that added this file).
// So: a from-scratch PNG decoder/encoder and ICO container builder, using
// only Node's built-in `zlib` for the deflate/inflate the PNG spec already
// requires.
//
// SCOPE, ON PURPOSE. This only understands the one PNG shape the shipped
// brand master actually is: 8-bit depth, colour type 6 (truecolour + alpha,
// i.e. RGBA), non-interlaced. That covers every filter type (0-4) the real
// master's rows use. Anything else (palettes, 16-bit depth, interlacing,
// no alpha) throws loudly with the values it saw — a silently-wrong icon is
// worse than a build that refuses.
//
// CONTRACT PORTED FROM `packaging/make-win-icon.ps1`, byte for byte: sizes
// [16, 32, 48, 64, 128, 256], ICONDIR (reserved=0, type=1, count),
// 16-byte ICONDIRENTRY records (256 encoded as byte 0, planes=1, bpp=32,
// data size, data offset), directory first then payloads — so for the fixed
// 6-entry set here the first payload always lands at 6 + 16*6 = 102.

import zlib from "node:zlib";

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const ICO_SIZES = [16, 32, 48, 64, 128, 256];

// --- CRC-32 (ISO 3309 / PNG's chunk CRC — same polynomial as zip/gzip) --- //
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) {
    c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

function paeth(a, b, c) {
  const p = a + b - c;
  const pa = Math.abs(p - a);
  const pb = Math.abs(p - b);
  const pc = Math.abs(p - c);
  if (pa <= pb && pa <= pc) return a;
  if (pb <= pc) return b;
  return c;
}

/**
 * Decode an 8-bit RGBA, non-interlaced PNG into raw pixels.
 * Throws (naming the offending values) on any other PNG shape.
 */
export function decodePng(buf) {
  if (buf.length < 8 || !buf.subarray(0, 8).equals(PNG_SIGNATURE)) {
    throw new Error("not a PNG file (bad signature)");
  }
  let pos = 8;
  let width;
  let height;
  let bitDepth;
  let colorType;
  let interlace;
  const idatChunks = [];
  while (pos + 8 <= buf.length) {
    const len = buf.readUInt32BE(pos);
    const type = buf.toString("ascii", pos + 4, pos + 8);
    const dataStart = pos + 8;
    const data = buf.subarray(dataStart, dataStart + len);
    if (type === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data[8];
      colorType = data[9];
      interlace = data[12];
    } else if (type === "IDAT") {
      idatChunks.push(data);
    } else if (type === "IEND") {
      break;
    }
    pos = dataStart + len + 4; // skip the chunk's trailing CRC
  }
  if (width === undefined) {
    throw new Error("no IHDR chunk found — not a valid PNG");
  }
  if (bitDepth !== 8 || colorType !== 6 || interlace !== 0) {
    throw new Error(
      `unsupported PNG shape: bitDepth=${bitDepth} colorType=${colorType} interlace=${interlace} ` +
        "(this decoder only supports 8-bit RGBA, non-interlaced — the shape the shipped brand master is)",
    );
  }

  const raw = zlib.inflateSync(Buffer.concat(idatChunks));
  const bpp = 4; // bytes per pixel for 8-bit RGBA
  const stride = width * bpp;
  const rgba = Buffer.alloc(height * stride);
  let rawPos = 0;
  let prevRow = Buffer.alloc(stride); // implicit all-zero row above the first
  for (let y = 0; y < height; y++) {
    const filterType = raw[rawPos];
    rawPos += 1;
    const outRow = rgba.subarray(y * stride, y * stride + stride);
    for (let x = 0; x < stride; x++) {
      const rawByte = raw[rawPos + x];
      const a = x >= bpp ? outRow[x - bpp] : 0; // left
      const b = prevRow[x]; // up
      const c = x >= bpp ? prevRow[x - bpp] : 0; // upper-left
      let value;
      switch (filterType) {
        case 0:
          value = rawByte;
          break;
        case 1:
          value = rawByte + a;
          break;
        case 2:
          value = rawByte + b;
          break;
        case 3:
          value = rawByte + ((a + b) >> 1);
          break;
        case 4:
          value = rawByte + paeth(a, b, c);
          break;
        default:
          throw new Error(`unsupported PNG filter type ${filterType} on row ${y}`);
      }
      outRow[x] = value & 0xff;
    }
    rawPos += stride;
    prevRow = outRow;
  }
  return { width, height, rgba };
}

/**
 * Area-average (box filter) resize of an RGBA buffer to a square `size`.
 * Handles non-integer ratios (e.g. 512 -> 48) by weighting each source
 * pixel by how much of it falls inside the destination pixel's footprint.
 * No premultiplication: the brand mark sits on a transparent ground and a
 * straight per-channel average (alpha included) is a fine approximation for
 * an icon at these sizes.
 */
export function resizeRgba(src, w, h, size) {
  const bpp = 4;
  const out = Buffer.alloc(size * size * bpp);
  const scaleX = w / size;
  const scaleY = h / size;
  for (let oy = 0; oy < size; oy++) {
    const sy0 = oy * scaleY;
    const sy1 = (oy + 1) * scaleY;
    const y0 = Math.floor(sy0);
    const y1 = Math.min(h, Math.ceil(sy1));
    for (let ox = 0; ox < size; ox++) {
      const sx0 = ox * scaleX;
      const sx1 = (ox + 1) * scaleX;
      const x0 = Math.floor(sx0);
      const x1 = Math.min(w, Math.ceil(sx1));
      let rSum = 0;
      let gSum = 0;
      let bSum = 0;
      let aSum = 0;
      let wSum = 0;
      for (let sy = y0; sy < y1; sy++) {
        const wy = Math.min(sy + 1, sy1) - Math.max(sy, sy0);
        for (let sx = x0; sx < x1; sx++) {
          const wx = Math.min(sx + 1, sx1) - Math.max(sx, sx0);
          const weight = wx * wy;
          const idx = (sy * w + sx) * bpp;
          rSum += src[idx] * weight;
          gSum += src[idx + 1] * weight;
          bSum += src[idx + 2] * weight;
          aSum += src[idx + 3] * weight;
          wSum += weight;
        }
      }
      const oidx = (oy * size + ox) * bpp;
      out[oidx] = Math.round(rSum / wSum);
      out[oidx + 1] = Math.round(gSum / wSum);
      out[oidx + 2] = Math.round(bSum / wSum);
      out[oidx + 3] = Math.round(aSum / wSum);
    }
  }
  return out;
}

function pngChunk(type, data) {
  const typeBuf = Buffer.from(type, "ascii");
  const lenBuf = Buffer.alloc(4);
  lenBuf.writeUInt32BE(data.length, 0);
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([lenBuf, typeBuf, data, crcBuf]);
}

/**
 * Encode a `size`x`size` RGBA buffer as an 8-bit, non-interlaced PNG.
 * Every scanline uses filter type 0 (None) — simplest correct encoder, and
 * `zlib.deflateSync` is a pure function of its input, so the output is
 * byte-identical across runs (no timestamps, no randomness).
 */
export function encodePng(rgba, size) {
  const bpp = 4;
  const stride = size * bpp;
  const raw = Buffer.alloc((stride + 1) * size);
  for (let y = 0; y < size; y++) {
    raw[y * (stride + 1)] = 0; // filter type: None
    rgba.copy(raw, y * (stride + 1) + 1, y * stride, y * stride + stride);
  }
  const idatData = zlib.deflateSync(raw);
  const ihdrData = Buffer.alloc(13);
  ihdrData.writeUInt32BE(size, 0);
  ihdrData.writeUInt32BE(size, 4);
  ihdrData[8] = 8; // bit depth
  ihdrData[9] = 6; // colour type: truecolour + alpha
  ihdrData[10] = 0; // compression method
  ihdrData[11] = 0; // filter method
  ihdrData[12] = 0; // interlace method
  return Buffer.concat([
    PNG_SIGNATURE,
    pngChunk("IHDR", ihdrData),
    pngChunk("IDAT", idatData),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}

/**
 * Build a 6-entry, PNG-payload .ico from a master PNG buffer, porting
 * `packaging/make-win-icon.ps1`'s container contract exactly.
 */
export function buildIco(masterPngBuffer) {
  const { width, height, rgba } = decodePng(masterPngBuffer);
  const blobs = ICO_SIZES.map((size) => ({
    size,
    data: encodePng(resizeRgba(rgba, width, height, size), size),
  }));

  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0); // reserved
  header.writeUInt16LE(1, 2); // type: icon
  header.writeUInt16LE(blobs.length, 4); // count

  const dirEntries = [];
  const payloads = [];
  let offset = 6 + 16 * blobs.length; // directory comes first, payloads after
  for (const blob of blobs) {
    const entry = Buffer.alloc(16);
    const wh = blob.size >= 256 ? 0 : blob.size; // 256 is encoded as 0
    entry[0] = wh; // width
    entry[1] = wh; // height
    entry[2] = 0; // palette colour count
    entry[3] = 0; // reserved
    entry.writeUInt16LE(1, 4); // colour planes
    entry.writeUInt16LE(32, 6); // bits per pixel
    entry.writeUInt32LE(blob.data.length, 8);
    entry.writeUInt32LE(offset, 12);
    offset += blob.data.length;
    dirEntries.push(entry);
    payloads.push(blob.data);
  }
  return Buffer.concat([header, ...dirEntries, ...payloads]);
}

/**
 * Validate that `buf` is a well-formed 6-entry, PNG-payload .ico:
 * ICONDIR header, first payload at offset 102, every payload starts with
 * the PNG magic, and a 256x256 entry is present (electron-builder's NSIS
 * target requires one). Throws a message naming the actual value on failure.
 */
export function validateIco(buf) {
  if (buf.length < 6) {
    throw new Error(`ico buffer too short (${buf.length} bytes)`);
  }
  const reserved = buf.readUInt16LE(0);
  const type = buf.readUInt16LE(2);
  const count = buf.readUInt16LE(4);
  if (reserved !== 0) throw new Error(`ico reserved field is ${reserved}, want 0`);
  if (type !== 1) throw new Error(`ico type field is ${type}, want 1`);
  if (count !== 6) throw new Error(`ico has ${count} entries, want 6`);

  let has256 = false;
  for (let i = 0; i < count; i++) {
    const entryOff = 6 + i * 16;
    const widthByte = buf[entryOff];
    const length = buf.readUInt32LE(entryOff + 8);
    const offset = buf.readUInt32LE(entryOff + 12);
    if (i === 0 && offset !== 102) {
      throw new Error(`first ico payload is at offset ${offset}, want 102`);
    }
    const payload = buf.subarray(offset, offset + length);
    if (!payload.subarray(0, 8).equals(PNG_SIGNATURE)) {
      throw new Error(`ico entry ${i} payload does not start with the PNG magic`);
    }
    if (widthByte === 0) has256 = true;
  }
  if (!has256) throw new Error("ico has no 256x256 entry (electron-builder's NSIS target requires one)");
  return true;
}
