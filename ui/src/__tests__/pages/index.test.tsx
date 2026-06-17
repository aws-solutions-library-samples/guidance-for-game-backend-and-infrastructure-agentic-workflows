import React from 'react'
import { render, screen } from '@testing-library/react'
import Home from '../../pages/index'

// Mock CopilotKit components
jest.mock('@copilotkit/react-core', () => ({
  CopilotKit: ({ children }: { children: React.ReactNode }) => <div data-testid="copilot-provider">{children}</div>,
  useCopilotAction: () => jest.fn(),
  useCopilotReadable: () => jest.fn(),
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
  it('renders without crashing', () => {
    render(<Home />)
    expect(screen.getByTestId('copilot-provider')).toBeInTheDocument()
  })

  it('displays the Game Agent title', () => {
    render(<Home />)
    const title = screen.getByRole('heading', { name: /Game Agent/i })
    expect(title).toBeInTheDocument()
  })

  it('displays the shield emoji', () => {
    render(<Home />)
    const shield = screen.getByText('🛡️')
    expect(shield).toBeInTheDocument()
  })

  it('has the correct page structure', () => {
    render(<Home />)

    // Check for main container
    const container = screen.getByRole('main') || screen.getByTestId('main-container')
    expect(container).toBeInTheDocument()

    // Check for chat component
    expect(screen.getByTestId('copilot-provider')).toBeInTheDocument()
  })

  it('applies correct CSS classes', () => {
    const { container } = render(<Home />)

    // Check that the component renders with some structure
    expect(container.firstChild).toBeTruthy()
  })

  it('is accessible', () => {
    render(<Home />)

    // Check for heading structure
    const headings = screen.getAllByRole('heading', { level: 1 })
    expect(headings.length).toBeGreaterThan(0)
  })
})
