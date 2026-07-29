import React, { useState } from 'react';

/**
 * Custom fenced-code renderer for the chat markdown.
 *
 * Replaces CopilotKit's bundled CodeBlock (via CopilotChat's `markdownTagRenderers`)
 * to fix two issues with the default:
 *   1. Contrast — the default relied on a highlight theme that rendered dark
 *      token text on the dark block background, making IaC/code unreadable.
 *      This block uses an explicit high-contrast dark surface (see chat-layout.css).
 *   2. Download filename — the default always suggested `file-<random>.file` for
 *      unrecognized languages (terraform/hcl/etc.), so the downloaded file never
 *      matched the filename shown in the UI. Here the download name is derived
 *      from the fence token: a token that already looks like a filename
 *      (e.g. ```main.tf) is used verbatim, otherwise a sensible extension is
 *      mapped from the language.
 */

// Map a bare language token to a file extension. Keys must be lowercase.
// Terraform/IaC languages are the important additions over the default map.
const LANGUAGE_EXTENSIONS: Record<string, string> = {
  terraform: '.tf',
  hcl: '.tf',
  tf: '.tf',
  yaml: '.yaml',
  yml: '.yml',
  json: '.json',
  bash: '.sh',
  shell: '.sh',
  sh: '.sh',
  dockerfile: 'Dockerfile',
  python: '.py',
  py: '.py',
  javascript: '.js',
  js: '.js',
  typescript: '.ts',
  ts: '.ts',
  go: '.go',
  java: '.java',
  ruby: '.rb',
  rust: '.rs',
  sql: '.sql',
  html: '.html',
  css: '.css',
  xml: '.xml',
  toml: '.toml',
  ini: '.ini',
  markdown: '.md',
  md: '.md',
};

/**
 * Derive a download filename from the code fence token.
 * - "main.tf" (contains a dot) -> used verbatim, so the download matches the label
 * - "hcl" / "terraform"        -> "code.tf"
 * - "dockerfile"               -> "Dockerfile"
 * - unknown / empty            -> "<token>.txt" or "code.txt"
 */
export function deriveFileName(token: string): string {
  if (!token) return 'code.txt';
  if (token.includes('.')) return token;
  const ext = LANGUAGE_EXTENSIONS[token.toLowerCase()];
  if (ext === 'Dockerfile') return 'Dockerfile';
  return ext ? `code${ext}` : `${token}.txt`;
}

const DownloadIcon = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <polyline points="7 10 12 15 17 10" />
    <line x1="12" y1="15" x2="12" y2="3" />
  </svg>
);

const CopyIcon = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </svg>
);

const CheckIcon = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <polyline points="20 6 9 17 4 12" />
  </svg>
);

// Matches the props react-markdown (v8) passes to a `code` component override.
interface ChatCodeBlockProps {
  inline?: boolean;
  className?: string;
  children?: React.ReactNode;
}

export function ChatCodeBlock({ inline, className, children }: ChatCodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const token = /language-([\w.+-]+)/.exec(className || '')?.[1] ?? '';
  const code = String(children ?? '').replace(/\n$/, '');

  // Inline code (no language fence and no newlines): render a simple readable pill.
  if (inline || (!token && !code.includes('\n'))) {
    return <code className="ga-inline-code">{children}</code>;
  }

  const fileName = deriveFileName(token);

  const handleCopy = async () => {
    if (copied) return;
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API can reject (permissions/insecure context); ignore silently.
    }
  };

  const handleDownload = () => {
    if (typeof window === 'undefined') return;
    const blob = new Blob([code], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = fileName;
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="ga-code-block">
      <div className="ga-code-block__toolbar">
        <span className="ga-code-block__lang">{token || 'text'}</span>
        <div className="ga-code-block__actions">
          <button
            type="button"
            className="ga-code-block__btn"
            onClick={handleDownload}
            title={`Download ${fileName}`}
            aria-label={`Download ${fileName}`}
          >
            {DownloadIcon}
          </button>
          <button
            type="button"
            className="ga-code-block__btn"
            onClick={handleCopy}
            title={copied ? 'Copied' : 'Copy code'}
            aria-label={copied ? 'Copied' : 'Copy code'}
          >
            {copied ? CheckIcon : CopyIcon}
          </button>
        </div>
      </div>
      <pre className="ga-code-block__pre">
        <code>{code}</code>
      </pre>
    </div>
  );
}

export default ChatCodeBlock;
