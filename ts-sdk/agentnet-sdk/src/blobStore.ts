import { promises as fs } from "node:fs";
import { join, basename, extname } from "node:path";
import { createHash } from "node:crypto";
import { AgentNetError } from "./types.js";

export interface BlobRef {
  type: "blob_ref";
  blob_id: string;
  storage: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
}

export function isBlobRef(payload: unknown): payload is BlobRef {
  if (!payload || typeof payload !== "object") return false;
  const p = payload as Record<string, unknown>;
  return p.type === "blob_ref" && typeof p.blob_id === "string";
}

export function parseBlobRef(payload: unknown): BlobRef {
  if (!isBlobRef(payload)) {
    throw new AgentNetError("Payload is not a valid blob_ref", { code: "invalid_blob_ref" });
  }
  return payload as BlobRef;
}

export interface BlobStoreOptions {
  baseDir?: string;
}

export class LocalBlobStore {
  private readonly baseDir: string;

  constructor(options?: BlobStoreOptions) {
    this.baseDir = options?.baseDir ?? process.env.AGENTNET_BLOB_DIR ?? ".agentnet_blobs";
  }

  private async ensureDir(): Promise<void> {
    await fs.mkdir(this.baseDir, { recursive: true });
  }

  private getBlobPath(blobId: string): string {
    return join(this.baseDir, blobId);
  }

  private getMetaPath(blobId: string): string {
    return join(this.baseDir, `${blobId}.json`);
  }

  async putBlobBytes(bytes: Uint8Array, filename: string, mimeType = "application/octet-stream"): Promise<BlobRef> {
    await this.ensureDir();
    const hash = createHash("sha256").update(bytes).digest("hex");
    const blobId = `blob_${hash.slice(0, 16)}_${Date.now()}`;
    const sizeBytes = bytes.length;

    const ref: BlobRef = {
      type: "blob_ref",
      blob_id: blobId,
      storage: "local_fs",
      filename: basename(filename),
      mime_type: mimeType,
      size_bytes: sizeBytes,
      sha256: hash,
      created_at: new Date().toISOString(),
    };

    await fs.writeFile(this.getBlobPath(blobId), bytes);
    await fs.writeFile(this.getMetaPath(blobId), JSON.stringify(ref, null, 2), "utf-8");

    return ref;
  }

  async putBlobFile(filePath: string): Promise<BlobRef> {
    const bytes = await fs.readFile(filePath);
    const filename = basename(filePath);
    const ext = extname(filename).toLowerCase();
    
    let mimeType = "application/octet-stream";
    if (ext === ".png") mimeType = "image/png";
    else if (ext === ".jpg" || ext === ".jpeg") mimeType = "image/jpeg";
    else if (ext === ".gif") mimeType = "image/gif";
    else if (ext === ".webp") mimeType = "image/webp";
    else if (ext === ".txt") mimeType = "text/plain";
    else if (ext === ".json") mimeType = "application/json";
    else if (ext === ".html") mimeType = "text/html";
    else if (ext === ".pdf") mimeType = "application/pdf";

    return this.putBlobBytes(bytes, filename, mimeType);
  }

  async getBlobBytes(blobId: string): Promise<Uint8Array> {
    try {
      return await fs.readFile(this.getBlobPath(blobId));
    } catch (err: any) {
      if (err.code === "ENOENT") {
        throw new AgentNetError(`Blob not found: ${blobId}`, { code: "blob_not_found" });
      }
      throw err;
    }
  }

  async getBlobText(blobId: string): Promise<string> {
    const bytes = await this.getBlobBytes(blobId);
    return new TextDecoder().decode(bytes);
  }

  async headBlob(blobId: string): Promise<BlobRef> {
    try {
      const metaJson = await fs.readFile(this.getMetaPath(blobId), "utf-8");
      return JSON.parse(metaJson) as BlobRef;
    } catch (err: any) {
      if (err.code === "ENOENT") {
        throw new AgentNetError(`Blob metadata not found: ${blobId}`, { code: "blob_not_found" });
      }
      throw err;
    }
  }

  async deleteBlob(blobId: string): Promise<void> {
    try {
      await fs.unlink(this.getBlobPath(blobId));
    } catch (err: any) {
      if (err.code !== "ENOENT") throw err;
    }
    try {
      await fs.unlink(this.getMetaPath(blobId));
    } catch (err: any) {
      if (err.code !== "ENOENT") throw err;
    }
  }
}
