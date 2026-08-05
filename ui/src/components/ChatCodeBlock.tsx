import React, { useState } from 'react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';

/**
 * Custom fenced-code renderer for the chat markdown.
 *
 * Wired through CopilotChat's `markdownTagRenderers` to fix issues with the
 * bundled CodeBlock while preserving its useful behavior:
 *   1. Readability — the block renders on an explicit high-contrast dark
 *      surface (see chat-layout.css) so IaC/code isn't dark-on-dark.
 *   2. Syntax highlighting — preserved via react-syntax-highlighter (Prism +
 *      vsc-dark-plus), matching CopilotKit's original highlighting.
 *   3. Download filename — derived from the fence token: a filename-like token
 *      (e.g. ```main.tf) is used verbatim so the download matches the label,
 *      otherwise the language maps to an extension. CopilotKit's full
 *      language->extension map is preserved so nothing regresses to `.txt`.
 */

// Language -> file extension. Every mapping from CopilotKit's bundled
// programmingLanguages map is preserved (so cpp/php/kotlin/swift/etc. keep
// their native extensions), plus Terraform/IaC and config additions.
const LANGUAGE_EXTENSIONS: Record<string, string> = {
  // Preserved from CopilotKit's programmingLanguages map:
  javascript: '.js',
  python: '.py',
  java: '.java',
  c: '.c',
  cpp: '.cpp',
  'c++': '.cpp',
  'c#': '.cs',
  ruby: '.rb',
  php: '.php',
  swift: '.swift',
  'objective-c': '.m',
  kotlin: '.kt',
  typescript: '.ts',
  go: '.go',
  perl: '.pl',
  rust: '.rs',
  scala: '.scala',
  haskell: '.hs',
  lua: '.lua',
  shell: '.sh',
  sql: '.sql',
  html: '.html',
  css: '.css',
  // Additions (Terraform/IaC, config, and common aliases):
  terraform: '.tf',
  hcl: '.tf',
  tf: '.tf',
  yaml: '.yaml',
  yml: '.yml',
  json: '.json',
  bash: '.sh',
  sh: '.sh',
  dockerfile: 'Dockerfile',
  py: '.py',
  js: '.js',
  ts: '.ts',
  rb: '.rb',
  rs: '.rs',
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
 * - "cpp" / "swift" / "kotlin" -> "code.cpp" / "code.swift" / "code.kt"
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

// Map a fence token to a Prism language id for highlighting. Filename-like
// tokens (main.tf) are reduced to a language by their extension.
const TOKEN_TO_PRISM: Record<string, string> = {
  tf: 'hcl',
  terraform: 'hcl',
  hcl: 'hcl',
  py: 'python',
  js: 'javascript',
  ts: 'typescript',
  rb: 'ruby',
  yml: 'yaml',
  sh: 'bash',
  shell: 'bash',
  md: 'markdown',
  dockerfile: 'docker',
};

export function deriveHighlightLanguage(token: string): string {
  if (!token) return 'text';
  const t = token.toLowerCase();
  const key = t.includes('.') ? t.split('.').pop() || '' : t;
  return TOKEN_TO_PRISM[key] || key || 'text';
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
  // Classify from the ORIGINAL content, before trimming the trailing newline.
  // react-markdown supplies one-line fenced blocks as e.g. "echo hello\n";
  // trimming first would make an unlabeled one-liner look inline and lose its
  // block styling, copy, and download controls.
  const rawChildren = String(children ?? '');
  const isBlock = !inline && (Boolean(token) || rawChildren.includes('\n'));

  if (!isBlock) {
    return <code className="ga-inline-code">{children}</code>;
  }

  const code = rawChildren.replace(/\n$/, '');
  const fileName = deriveFileName(token);
  const highlightLanguage = deriveHighlightLanguage(token);

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
      <SyntaxHighlighter
        language={highlightLanguage}
        style={vscDarkPlus}
        PreTag="div"
        className="ga-code-block__pre"
        customStyle={{
          margin: 0,
          padding: '14px 16px',
          background: '#1e1e1e',
          fontSize: '13px',
          lineHeight: '1.55',
        }}
        codeTagProps={{
          style: { fontFamily: 'Menlo, Monaco, Consolas, "Courier New", monospace' },
        }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}

export default ChatCodeBlock;
