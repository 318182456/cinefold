/**
 * 极简 Markdown 渲染，只覆盖助手实际会输出的那几种：
 * 标题、表格、有序/无序列表、粗体、行内代码、代码块、链接。
 *
 * 结果要喂给 v-html，所以先整体转义再逐条替换 —— 助手会把日志原文和影片
 * 标题带出来，里面完全可能有 < > 甚至标签，不转义就是个注入口子。
 */

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** 行内元素。传进来的必须是已转义的文本。 */
function inline(text) {
  return text
    // 代码优先，避免里面的星号被当成粗体
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    // 转义后链接里的引号已成实体，这里只允许 http(s)，挡掉 javascript:
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
    )
}

function splitRow(line) {
  return line
    .replace(/^\s*\|/, '')
    .replace(/\|\s*$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

const isTableRow = (line) => /^\s*\|.*\|\s*$/.test(line)
// 表格的分隔行，形如 |---|:--:|
const isTableSplit = (line) => /^\s*\|[\s:|-]+\|\s*$/.test(line)

export function renderMarkdown(source) {
  if (!source) return ''

  const lines = escapeHtml(source).split('\n')
  const out = []
  let index = 0
  // 连续的列表项要包进同一个 ul/ol，用它记住当前开着哪个
  let listTag = ''

  const closeList = () => {
    if (listTag) {
      out.push(`</${listTag}>`)
      listTag = ''
    }
  }

  while (index < lines.length) {
    const line = lines[index]

    // 代码块：找到闭合的 ``` 之前原样收下
    if (/^\s*```/.test(line)) {
      closeList()
      const buffer = []
      index += 1
      while (index < lines.length && !/^\s*```/.test(lines[index])) {
        buffer.push(lines[index])
        index += 1
      }
      index += 1
      out.push(`<pre><code>${buffer.join('\n')}</code></pre>`)
      continue
    }

    // 表格：表头 + 分隔行 + 若干数据行
    if (isTableRow(line) && index + 1 < lines.length && isTableSplit(lines[index + 1])) {
      closeList()
      const head = splitRow(line).map((cell) => `<th>${inline(cell)}</th>`).join('')
      index += 2
      const body = []
      while (index < lines.length && isTableRow(lines[index])) {
        const cells = splitRow(lines[index]).map((cell) => `<td>${inline(cell)}</td>`).join('')
        body.push(`<tr>${cells}</tr>`)
        index += 1
      }
      out.push(
        '<div class="md-table-wrap"><table>' +
          `<thead><tr>${head}</tr></thead><tbody>${body.join('')}</tbody>` +
          '</table></div>',
      )
      continue
    }

    const ordered = line.match(/^\s*\d+\.\s+(.*)$/)
    const bullet = line.match(/^\s*[-*]\s+(.*)$/)
    if (ordered || bullet) {
      const tag = ordered ? 'ol' : 'ul'
      if (listTag !== tag) {
        closeList()
        out.push(`<${tag}>`)
        listTag = tag
      }
      out.push(`<li>${inline((ordered || bullet)[1])}</li>`)
      index += 1
      continue
    }

    const heading = line.match(/^\s*(#{1,4})\s+(.*)$/)
    if (heading) {
      closeList()
      out.push(`<p><strong>${inline(heading[2])}</strong></p>`)
      index += 1
      continue
    }

    if (!line.trim()) {
      closeList()
      index += 1
      continue
    }

    closeList()
    out.push(`<p>${inline(line)}</p>`)
    index += 1
  }

  closeList()
  return out.join('')
}

export default renderMarkdown
