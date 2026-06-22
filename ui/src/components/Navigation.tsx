import { useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { logError } from '@/utils/logger';
import { fetchWithTimeout } from '@/utils/fetchWithTimeout';

interface NavigationProps {
  pageTitle?: string;
  showBackButton?: boolean;
}

export default function Navigation({ pageTitle, showBackButton = false }: NavigationProps) {
  const [showMenu, setShowMenu] = useState(false);
  const [userInfo, setUserInfo] = useState<{ username: string; email: string; isAdmin: boolean } | null>(null);
  const router = useRouter();

  // Fetch user info when component mounts
  useEffect(() => {
    fetchWithTimeout('/api/auth/user')
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data) {
          setUserInfo(data);
        }
      })
      .catch(() => {
        setUserInfo({ username: 'User', email: '', isAdmin: false });
      });
  }, []);

  const handleSignOut = async () => {
    try {
      await fetchWithTimeout('/api/auth/logout', { method: 'POST' });
      setUserInfo(null);
      window.location.href = '/';
    } catch (error) {
      logError('Logout failed:', error instanceof Error ? error : new Error(String(error)));
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

  const username = userInfo?.username || 'User';

  return (
    <header className="ga-nav-header">
      <div className="ga-nav-content">
        <div className="ga-nav-left">
          <Link href="/" className="ga-logo-link">
            <div className="ga-logo">🛡️</div>
            <div className="ga-branding">
              <h1>Game Agent</h1>
              {pageTitle && <p className="ga-page-title">{pageTitle}</p>}
            </div>
          </Link>

          {showBackButton && (
            <button
              onClick={() => router.push('/')}
              className="ga-back-button"
            >
              ← Back to Chat
            </button>
          )}
        </div>

        <div className="ga-nav-right">
          <div className="ga-status-badge">
            <div className="ga-status-dot"></div>
            <span>System Online</span>
          </div>

          {userInfo && (
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
                    <div className="ga-user-email">{userInfo.email || username}</div>
                  </div>

                  {(userInfo.isAdmin) && router.pathname !== '/admin/users' && (
                    <Link
                      href="/admin/users"
                      className="ga-admin-link"
                      onClick={() => setShowMenu(false)}
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

      <style jsx>{`
        .ga-nav-header {
          background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
          border-bottom: 1px solid rgba(255, 255, 255, 0.1);
          padding: 16px 0;
          position: relative;
          overflow: visible;
          z-index: 1000;
        }

        .ga-nav-header::before {
          content: '';
          position: absolute;
          top: 0;
          left: 0;
          right: 0;
          bottom: 0;
          background: linear-gradient(90deg, transparent, rgba(139, 69, 255, 0.1), transparent);
          animation: navScan 4s infinite;
        }

        @keyframes navScan {
          0% { transform: translateX(-100%); }
          100% { transform: translateX(100%); }
        }

        .ga-nav-content {
          max-width: 1200px;
          margin: 0 auto;
          padding: 0 24px;
          display: grid;
          grid-template-columns: 1fr auto 1fr;
          align-items: center;
          position: relative;
          z-index: 1;
          min-height: 64px;
        }

        .ga-nav-left {
          display: flex;
          align-items: center;
          gap: 24px;
          justify-self: start;
          margin-left: 16px;
        }

        .ga-logo-link {
          display: flex;
          align-items: center;
          gap: 16px;
          text-decoration: none;
          color: inherit;
        }

        .ga-logo {
          width: 48px;
          height: 48px;
          background: linear-gradient(135deg, #8b45ff 0%, #3d5afe 100%);
          border-radius: 12px;
          display: flex;
          align-items: center;
          justify-content: center;
          box-shadow: 0 4px 16px rgba(139, 69, 255, 0.3);
          font-size: 24px;
          flex-shrink: 0;
        }

        .ga-branding h1 {
          margin: 0;
          font-size: 1.5rem;
          font-weight: 700;
          background: linear-gradient(135deg, #8b45ff 0%, #3d5afe 50%, #00bcd4 100%);
          -webkit-background-clip: text;
          -webkit-text-fill-color: transparent;
          background-clip: text;
          white-space: nowrap;
        }

        .ga-page-title {
          margin: 2px 0 0 0;
          color: rgba(255, 255, 255, 0.7);
          font-size: 0.85rem;
          font-weight: 500;
        }

        .ga-back-button {
          background: rgba(139, 69, 255, 0.2);
          border: 1px solid rgba(139, 69, 255, 0.4);
          border-radius: 8px;
          padding: 8px 16px;
          color: #8b45ff;
          font-size: 14px;
          font-weight: 500;
          cursor: pointer;
          transition: all 0.2s;
        }

        .ga-back-button:hover {
          background: rgba(139, 69, 255, 0.3);
          border-color: rgba(139, 69, 255, 0.6);
        }

        .ga-nav-right {
          display: flex;
          align-items: center;
          gap: 20px;
          justify-self: end;
        }

        .ga-status-badge {
          background: rgba(0, 0, 0, 0.3);
          border: 1px solid rgba(255, 255, 255, 0.1);
          border-radius: 20px;
          padding: 8px 14px;
          display: flex;
          align-items: center;
          gap: 8px;
          font-size: 12px;
          color: rgba(255, 255, 255, 0.8);
        }

        .ga-status-dot {
          width: 8px;
          height: 8px;
          background: #00ff88;
          border-radius: 50%;
          /* Solid green - no animation when system is stable */
        }

        .ga-user-menu {
          position: relative;
        }

        .ga-user-button {
          background: rgba(0, 0, 0, 0.3);
          border: 1px solid rgba(255, 255, 255, 0.1);
          border-radius: 25px;
          padding: 6px 16px 6px 6px;
          display: flex;
          align-items: center;
          gap: 10px;
          cursor: pointer;
          transition: all 0.2s;
          color: white;
        }

        .ga-user-button:hover {
          background: rgba(139, 69, 255, 0.2);
          border-color: rgba(139, 69, 255, 0.4);
        }

        .ga-user-avatar {
          width: 32px;
          height: 32px;
          background: linear-gradient(135deg, #8b45ff 0%, #3d5afe 100%);
          border-radius: 50%;
          display: flex;
          align-items: center;
          justify-content: center;
          font-weight: 600;
          font-size: 14px;
        }

        .ga-user-name {
          font-size: 14px;
          font-weight: 500;
        }

        /* Mobile Responsiveness */
        @media (max-width: 768px) {
          .ga-nav-content {
            padding: 0 16px;
            grid-template-columns: 1fr auto;
            gap: 12px;
          }

          .ga-nav-left {
            margin-left: 8px;
            gap: 16px;
          }

          .ga-logo {
            width: 40px;
            height: 40px;
            font-size: 20px;
          }

          .ga-branding h1 {
            font-size: 1.25rem;
          }

          .ga-page-title {
            font-size: 0.8rem;
          }

          .ga-back-button {
            padding: 6px 12px;
            font-size: 13px;
          }

          .ga-nav-right {
            gap: 12px;
          }

          .ga-status-badge {
            padding: 6px 10px;
            font-size: 11px;
            gap: 6px;
          }

          .ga-status-dot {
            width: 6px;
            height: 6px;
          }

          .ga-user-button {
            padding: 4px 12px 4px 4px;
            gap: 8px;
          }

          .ga-user-avatar {
            width: 28px;
            height: 28px;
            font-size: 12px;
          }

          .ga-user-name {
            font-size: 13px;
          }

          .ga-user-dropdown {
            right: -8px;
            min-width: 180px;
            padding: 12px;
          }
        }

        @media (max-width: 480px) {
          .ga-nav-content {
            padding: 0 12px;
          }

          .ga-nav-left {
            margin-left: 4px;
            gap: 12px;
          }

          .ga-branding h1 {
            font-size: 1.1rem;
          }

          .ga-page-title {
            display: none;
          }

          .ga-nav-right {
            gap: 8px;
          }

          .ga-status-badge span {
            display: none;
          }

          .ga-status-badge {
            padding: 8px;
            min-width: 20px;
            justify-content: center;
          }

          .ga-user-name {
            display: none;
          }

          .ga-user-button {
            padding: 4px;
            min-width: 36px;
            justify-content: center;
          }
        }

        .ga-user-dropdown {
          position: absolute;
          top: 100%;
          right: 0;
          margin-top: 8px;
          background: rgba(26, 26, 46, 0.95);
          border: 1px solid rgba(139, 69, 255, 0.3);
          border-radius: 12px;
          padding: 16px;
          min-width: 200px;
          box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
          backdrop-filter: blur(10px);
          z-index: 1000;
        }

        .ga-user-info {
          padding-bottom: 12px;
          border-bottom: 1px solid rgba(139, 69, 255, 0.2);
          margin-bottom: 12px;
        }

        .ga-user-email {
          color: rgba(255, 255, 255, 0.8);
          font-size: 13px;
        }

        .ga-admin-link {
          display: block;
          padding: 8px 12px;
          color: #8B45FF;
          text-decoration: none;
          font-size: 14px;
          font-weight: 500;
          border-radius: 6px;
          margin-bottom: 8px;
          transition: background 0.2s;
        }

        .ga-admin-link:hover {
          background: rgba(139, 69, 255, 0.1);
        }

        .ga-signout-button {
          width: 100%;
          padding: 8px 12px;
          background: rgba(255, 69, 69, 0.2);
          border: 1px solid rgba(255, 69, 69, 0.3);
          border-radius: 6px;
          color: #ff6b6b;
          font-size: 14px;
          font-weight: 500;
          cursor: pointer;
          transition: all 0.2s;
        }

        .ga-signout-button:hover {
          background: rgba(255, 69, 69, 0.3);
          border-color: rgba(255, 69, 69, 0.5);
        }
      `}</style>
    </header>
  );
}
