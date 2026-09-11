import React from 'react';
import { ChatCodeBlock } from '@/components/ChatCodeBlock';
import { ChartRenderer } from '@/components/charts/ChartRenderer';
import { parseChartSpec } from '@/components/charts/chartContract';

/**
 * Markdown `code` renderer wired into CopilotChat's `markdownTagRenderers`.
 *
 * It adds inline chart support (issue #255) on top of the existing fenced-code
 * behavior without changing how ordinary code blocks render:
 *
 *   - A fenced block tagged ```chart whose body validates against the chart
 *     contract renders as an inline SVG chart ({@link ChartRenderer}).
 *   - Anything else — including a ```chart block whose body is malformed,
 *     truncated (mid-stream), or otherwise out of contract — falls through to
 *     {@link ChatCodeBlock}, so the raw payload is shown as inert, readable
 *     text. Model output is never executed and never rendered as HTML.
 *
 * Failing closed to the code block (rather than throwing or hiding content)
 * keeps the chat resilient to partial streaming and to untrusted payloads.
 */

interface MarkdownCodeRendererProps {
  inline?: boolean;
  className?: string;
  children?: React.ReactNode;
}

export function MarkdownCodeRenderer(props: MarkdownCodeRendererProps) {
  const { inline, className, children } = props;
  const token = /language-([\w.+-]+)/.exec(className || '')?.[1] ?? '';

  if (!inline && token.toLowerCase() === 'chart') {
    const body = String(children ?? '').replace(/\n$/, '');
    const spec = parseChartSpec(body);
    if (spec) {
      return <ChartRenderer spec={spec} />;
    }
    // Invalid/partial chart payload: fall through to the code block so the raw
    // JSON is visible (and copy/downloadable) rather than silently dropped.
  }

  return <ChatCodeBlock {...props} />;
}

export default MarkdownCodeRenderer;
