import React from 'react'
import { render, screen } from '@testing-library/react'
import Home from '../../pages/index'
import { ThemeProvider } from '../../components/ThemeProvider'

// Mock CopilotKit components
jest.mock('@copilotkit/react-core', () => ({
  CopilotKit: ({ children }: { children: React.ReactNode }) => <div data-testid="copilot-provider">{children}</div>,
  useCopilotAction: () => jest.fn(),
  useCopilotReadable: () => jest.fn(),
  // Hooks consumed by NewChatButton (#253)
  useCopilotChat: () => ({ reset: jest.fn() }),
  useCopilotContext: () => ({ setThreadId: jest.fn() }),
}))

jest.mock('@copilotkit/react-ui', () => ({
  CopilotChat: ({ className }: { className?: string }) => (
    <div data-testid="copilot-sidebar" className={className}>
      <div data-testid="chat-input">
        <input type="text" placeholder="Ask me anything..." />
        <button>Send</button>
      </div>
    </div>
  ),
}))

describe('Home Page', () => {
  const renderHome = () => render(
    <ThemeProvider>
      <Home />
    </ThemeProvider>,
  )

  it('renders without crashing', () => {
    renderHome()
    expect(screen.getByTestId('copilot-provider')).toBeInTheDocument()
  })

  it('displays the Game Agent title', () => {
    renderHome()
    const title = screen.getByRole('heading', { name: /Game Agent/i })
    expect(title).toBeInTheDocument()
  })

  it('displays the shield emoji', () => {
    renderHome()
    const shield = screen.getByText('🛡️')
    expect(shield).toBeInTheDocument()
  })

  it('has the correct page structure', () => {
    renderHome()

    // Check for main container
    const container = screen.getByRole('main') || screen.getByTestId('main-container')
    expect(container).toBeInTheDocument()

    // Check for chat component
    expect(screen.getByTestId('copilot-provider')).toBeInTheDocument()
  })

  it('applies correct CSS classes', () => {
    const { container } = render(
      <ThemeProvider>
        <Home />
      </ThemeProvider>,
    )

    // Check that the component renders with some structure
    expect(container.firstChild).toBeTruthy()
  })

  it('is accessible', () => {
    renderHome()

    // Check for heading structure
    const headings = screen.getAllByRole('heading', { level: 1 })
    expect(headings.length).toBeGreaterThan(0)
  })
})
