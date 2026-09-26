/**
 * 会话头部动作：**☀ 评审今日**（D1：把日复一日复述的评审 SOP 固化成一次点击）。
 *
 * 行为：把固定提示词**复制进剪贴板**并在按钮上自证（"已复制 ✓"），粘贴给 AI 即启动
 * 今日三段评审。刻意**不猜** dsh 有没有"代发消息"的注入 API——那是宿主侧能力，
 * 没有它"复制到剪贴板"就是零风险且真实有用的落点（失败也会说"复制失败"，不假装成功）。
 *
 * 文案与 W4（review_floor）/ W5（两阶段）/ 空池 requeue 对齐——改协议时改这里。
 */
import { useState } from 'react'
import type { PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'

type Props = PropsRuntime<'conversation.session.header.actions'>

export const REVIEW_SOP_PROMPT = [
  '跑今天的 PaperPilot 评审（全程用 mcp__paperpilot__ 工具，别开浏览器）：',
  '1) prepare_review()；若报 empty_pool：看 hint 里的计数，该 requeue 就 prepare_review(requeue=true)，该拉新就 fetch_papers(days=1) 再 prepare。',
  '2) 两阶段：先 stage=brief 粗筛（标题+短摘），选 ≤15 篇；再 stage=full + arxiv_ids 拉全文精评。',
  '3) submit_review(reviews=[{arxiv_id,score,label,reason,入选者带 summary}])——不传 date；坏项会单独进 rejected，别慌整批。',
  '4) finalize_briefing(force=true) → read_digest() 验收；顺手报一句本次 token 量级与入选清单。',
  '写前如被 authority_locked 拒：read_authority 看怎么写权开闸（需人类侧口令，别绕门）。',
].join('\n')

export function ReviewSopButton(_props: Props) {
  const [copied, setCopied] = useState<null | boolean>(null)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(REVIEW_SOP_PROMPT)
      setCopied(true)
    } catch {
      setCopied(false)          // 响亮：失败也说失败，不假装成功
    }
  }
  return (
    <button
      type="button"
      onClick={() => void copy()}
      data-pp-review-sop="on"
      title={copied === false ? '复制失败——检查浏览器剪贴板权限' : '复制今日评审 SOP 提示词，粘贴给 AI 即可开工'}
    >
      {copied ? '已复制 ✓' : '☀ 评审今日'}
    </button>
  )
}
