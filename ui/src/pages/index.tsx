/**
 * Home Page for Game Agent - Command Center Layout
 */

import React, { useState, useEffect } from 'react';
import Head from 'next/head';
import Link from 'next/link';
import { Chat } from '../components/Chat';
import ThemeToggle from '../components/ThemeToggle';
import { fetchWithTimeout } from '@/utils/fetchWithTimeout';

interface HomeProps {
  user?: {
    username?: string;
    email?: string;
    isAdmin?: boolean;
  };
}

/**
 * Home page component with Command Center layout
 */
export default function Home({ user }: HomeProps) {
  const [showMenu, setShowMenu] = useState(false);
  const [userInfo, setUserInfo] = useState<{ username: string; email: string; isAdmin: boolean } | null>(null);
  const [isAIThinking, setIsAIThinking] = useState(false);

  // Fetch user info from API when user is authenticated
  useEffect(() => {
    if (user) {
      fetchWithTimeout('/api/auth/user')
        .then(res => res.ok ? res.json() : null)
        .then(data => {
          if (data) {
            setUserInfo(data);
          }
        })
        .catch(() => {
          // Fallback if API fails
          setUserInfo({ username: 'User', email: '', isAdmin: false });
        });
    }
  }, [user]);

  const username = userInfo?.username || user?.username || 'User';

  const handleSignOut = async () => {
    try {
      await fetchWithTimeout('/api/auth/logout', { method: 'POST' });
      // Clear local state
      setUserInfo(null);
      // Force reload to clear all state
      window.location.href = '/';
    } catch (error) {
      console.error('Logout failed:', error);
      // Force reload anyway
      window.location.href = '/';
    }
  };

  // Close menu when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target as HTMLElement;
      if (showMenu && !target.closest('.ga-user-menu')) {
        setShowMenu(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showMenu]);

  return (
    <>
      <Head>
        <title>Game Agent - AI-Powered Game Server Management</title>
        <meta name="description" content="AI-powered assistant for managing AWS GameLift fleets and EKS clusters" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
      </Head>
      <div className="ga-layout">
      {/* Hero Header Section */}
      <header className="ga-hero-header">
        <div className="ga-hero-content">
          <div className="ga-logo-container">
            <div className="ga-logo">🛡️</div>
            <div className="ga-branding">
              <h1>Game Agent</h1>
              <p>AI-Powered Game Server Management</p>
            </div>
          </div>

          <div className="ga-status-indicators">
            <ThemeToggle />
            <div className="ga-status-badge">
              <div className="ga-status-dot"></div>
              <span>System Online</span>
            </div>
            <div className={`ga-status-badge ${isAIThinking ? 'thinking' : ''}`}>
              <div className="ga-status-dot"></div>
              <span>{isAIThinking ? 'AI Thinking...' : 'AI Ready'}</span>
            </div>
            <div className="ga-status-badge memory-badge">
              <div className="ga-status-dot memory-dot"></div>
              <span>Memory Active</span>
            </div>
            <div className="ga-status-badge">
              <div className="ga-status-dot"></div>
              <span>Region: {process.env.NEXT_PUBLIC_AWS_REGION || 'us-west-2'}</span>
            </div>

            {user && (
              <div className="ga-user-menu">
                <button
                  className="ga-user-button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setShowMenu(!showMenu);
                  }}
                >
                  <div className="ga-user-avatar">
                    {username.charAt(0).toUpperCase()}
                  </div>
                  <span className="ga-user-name">{username}</span>
                </button>
                {showMenu && (
                  <div className="ga-user-dropdown">
                    <div className="ga-user-info">
                      <div className="ga-user-email">{userInfo?.email || username}</div>
                    </div>

                    {(userInfo?.isAdmin || user?.isAdmin) && (
                      <Link
                        href="/admin/users"
                        style={{
                          display: 'block',
                          padding: '12px 16px',
                          color: 'var(--ga-accent)',
                          textDecoration: 'none',
                          fontSize: '14px',
                          fontWeight: 500,
                          borderBottom: '1px solid var(--ga-accent-border)'
                        }}
                      >
                        Manage Users
                      </Link>
                    )}
                    <button
                      className="ga-signout-button"
                      onClick={handleSignOut}
                    >
                      Sign Out
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </header>

      {/* Main Content - Centered Chat */}
      <main className="ga-main">
        <div className="ga-chat-container">
          <div className="ga-chat-header">
            <div className="ga-chat-avatar">🤖</div>
            <div>
              <div className="ga-chat-title">Game Agent AI Assistant</div>
              <div className="ga-chat-subtitle">Ready to help with your game infrastructure</div>
            </div>
          </div>
          <Chat onThinkingChange={setIsAIThinking} />
        </div>
      </main>

      {/* Footer */}
      <footer className="ga-footer">
        <p>Game Agent Command Center</p>
      </footer>
    </div>
    </>
  );
}
