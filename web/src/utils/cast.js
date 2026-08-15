/* 演员名清洗。
 *
 * 数据源写进 casts 的东西并不都是名字：
 *
 *   javdb 中文站     野野浦暖 (野々浦暖)          译名 + 括号里的日文原名
 *   MGS 素人系       エミリサン 26歳 株トレーダー   艺名 + 年龄 + 职业设定
 *   两者叠加         エミリサン 26岁 株トレーダー (エミリサン 26歳 株トレーダー)
 *
 * 卡片上只想看到「野野浦暖」「エミリサン」。原始值仍然留在 title 属性里，
 * 需要核对时鼠标悬停能看到全文。
 *
 * 规则一律保守：只剥掉能确定是噪声的部分，判断不了就原样返回 —— 宁可多显示
 * 几个字，也不能把真名截断。演员名本身可能带空格（外国艺名、罗马音），
 * 所以不能见到空格就切。
 */

// 年龄：26歳 / 26岁 / 26才，前面可能有空格
const AGE = /\s*\d{1,3}\s*[歳岁才]/

// 职业・设定类后缀。MGS 这类站点把人物设定写进出演栏，都是固定的几种词尾
const OCCUPATION = /(トレーダー|エンジニア|デザイナー|アナウンサー|モデル|ナース|セラピスト|コンサル\w*|OL|学生|教師|主婦|店員|受付|秘書|社長|社員|マネージャー|スタッフ)\s*$/

/** 去掉括号补充说明：全角/半角都算，只处理结尾那一段 */
function stripParen(name) {
  return name.replace(/\s*[(（][^)）]*[)）]\s*$/, '').trim()
}

/**
 * 单个演员名的清洗。
 * @param {string} raw 原始值
 * @returns {string} 清洗后的名字；无法判断时返回去掉首尾空白的原值
 */
export function cleanCastName(raw) {
  let name = String(raw || '').trim()
  if (!name) return ''

  // 1. 先剥括号。javdb 的「译名 (原名)」和 MGS 叠加翻译后的重复都在这一步解决
  const withoutParen = stripParen(name)
  if (withoutParen) name = withoutParen

  // 2. 年龄之后的内容一律是设定，不是名字：
  //    「エミリサン 26歳 株トレーダー」→「エミリサン」
  const ageAt = name.search(AGE)
  if (ageAt > 0) {
    const head = name.slice(0, ageAt).trim()
    if (head) return head
  }

  // 3. 没写年龄但以职业词结尾的，切掉最后一段
  //    「リョウ 卑猥スギル下半身」这类无固定词尾的宣传语切不掉，只能留着 ——
  //    切错真名比多显示几个字更糟
  if (OCCUPATION.test(name)) {
    const head = name.replace(OCCUPATION, '').trim()
    if (head) return head
  }

  return name
}

/**
 * 把 casts 字段拆成干净的演员名数组。
 * @param {string} casts 逗号分隔的原始字段
 * @param {number} limit 最多返回几个
 */
export function parseCasts(casts, limit = 3) {
  const seen = new Set()
  const out = []
  for (const part of String(casts || '').split(',')) {
    const name = cleanCastName(part)
    // 清洗后可能和另一条重名（译名与原名并列时常见），去重
    if (!name || seen.has(name)) continue
    seen.add(name)
    out.push(name)
    if (out.length >= limit) break
  }
  return out
}
