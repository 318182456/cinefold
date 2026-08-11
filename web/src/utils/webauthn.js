/**
 * WebAuthn 浏览器端封装。
 *
 * 服务端用 base64url 传二进制，浏览器 API 要的是 ArrayBuffer，
 * 两个方向都得转一遍。
 */

export function isSupported() {
  return typeof window !== 'undefined'
    && typeof window.PublicKeyCredential === 'function'
    && typeof navigator.credentials?.create === 'function'
}

function base64urlToBuffer(value) {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/')
  const binary = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4))
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i])
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** 注册：把服务端选项转成浏览器要的格式 */
function prepareCreateOptions(options) {
  return {
    ...options,
    challenge: base64urlToBuffer(options.challenge),
    user: { ...options.user, id: base64urlToBuffer(options.user.id) },
    excludeCredentials: (options.excludeCredentials || []).map((c) => ({
      ...c,
      id: base64urlToBuffer(c.id),
    })),
  }
}

function prepareGetOptions(options) {
  return {
    ...options,
    challenge: base64urlToBuffer(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((c) => ({
      ...c,
      id: base64urlToBuffer(c.id),
    })),
  }
}

/** 把凭证转成服务端能验的 JSON */
function serializeRegistration(credential) {
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      attestationObject: bufferToBase64url(credential.response.attestationObject),
    },
    // 有些认证器不提供，缺了不影响验证
    transports: credential.response.getTransports?.() || [],
  }
}

function serializeAuthentication(credential) {
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      authenticatorData: bufferToBase64url(credential.response.authenticatorData),
      signature: bufferToBase64url(credential.response.signature),
      userHandle: credential.response.userHandle
        ? bufferToBase64url(credential.response.userHandle)
        : null,
    },
  }
}

/** 用户取消与真实错误要区分开，前者不该弹错误提示 */
export class PasskeyCancelled extends Error {}

function wrapError(err) {
  if (err?.name === 'NotAllowedError' || err?.name === 'AbortError') {
    return new PasskeyCancelled('已取消')
  }
  if (err?.name === 'InvalidStateError') {
    return new Error('这把钥匙已经注册过了')
  }
  if (err?.name === 'SecurityError') {
    return new Error('Passkey 需要 HTTPS，且域名要与配置一致')
  }
  return err
}

export async function createCredential(options) {
  try {
    const credential = await navigator.credentials.create({
      publicKey: prepareCreateOptions(options),
    })
    return serializeRegistration(credential)
  } catch (err) {
    throw wrapError(err)
  }
}

export async function getCredential(options) {
  try {
    const credential = await navigator.credentials.get({
      publicKey: prepareGetOptions(options),
    })
    return serializeAuthentication(credential)
  } catch (err) {
    throw wrapError(err)
  }
}
