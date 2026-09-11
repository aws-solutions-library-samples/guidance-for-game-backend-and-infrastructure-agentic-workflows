/**
 * Tests for MarkdownCodeRenderer (issue #255) — the markdown `code` dispatcher
 * that renders ```chart blocks as inline charts and falls back to the code
 * block for everything else (including malformed chart payloads).
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MarkdownCodeRenderer } from '../../components/MarkdownCodeRenderer';

const validChart = JSON.stringify({
  type: 'line',
  title: 'GameLift spend',
  summary: 'Trending up.',
  x: { values: ['Jun', 'Jul'] },
  series: [{ name: 'GameLift', values: [1, 2] }],
});

describe('MarkdownCodeRenderer', () => {
  it('renders a valid ```chart block as an inline chart', () => {
    const { container } = render(
      <MarkdownCodeRenderer className="language-chart">{`${validChart}\n`}</MarkdownCodeRenderer>
    );
    expect(screen.getByRole('img')).toBeInTheDocument();
    expect(container.querySelector('.ga-chart__title')).toHaveTextContent('GameLift spend');
  });

  it('falls back to a code block for a malformed chart payload (no execution, raw shown)', () => {
    render(
      <MarkdownCodeRenderer className="language-chart">{'{ not valid json\n'}</MarkdownCodeRenderer>
    );
    // No chart rendered.
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    // Fenced code-block controls are present, meaning the raw payload is shown.
    expect(screen.getByRole('button', { name: /copy code/i })).toBeInTheDocument();
  });

  it('falls back to a code block for a partial (mid-stream) chart payload', () => {
    render(
      <MarkdownCodeRenderer className="language-chart">{validChart.slice(0, 20)}</MarkdownCodeRenderer>
    );
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /copy code/i })).toBeInTheDocument();
  });

  it('delegates ordinary code fences to the code block unchanged', () => {
    render(
      <MarkdownCodeRenderer className="language-python">{'def handler():\n    return 1\n'}</MarkdownCodeRenderer>
    );
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByText('python')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /download code\.py/i })).toBeInTheDocument();
  });

  it('renders inline code unchanged', () => {
    render(<MarkdownCodeRenderer inline>fleetId</MarkdownCodeRenderer>);
    const el = screen.getByText('fleetId');
    expect(el.tagName).toBe('CODE');
    expect(el).toHaveClass('ga-inline-code');
  });

  it('does not treat an inline `chart` token as a chart', () => {
    // Defense-in-depth: inline code must never trigger chart rendering.
    render(<MarkdownCodeRenderer inline className="language-chart">{validChart}</MarkdownCodeRenderer>);
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
});
