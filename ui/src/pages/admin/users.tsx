import { useCallback, useState, useEffect } from 'react';
import Head from 'next/head';
import Navigation from '../../components/Navigation';

interface User {
  username: string;
  email: string;
  status: string;
  created: string;
  groups: string[];
}

export default function AdminUsers() {
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);

  // Declared before the effect that calls it (and memoized) so the rule of hooks
  // is satisfied and the effect dependency is stable.
  const fetchUsers = useCallback(async () => {
    const res = await fetch('/api/admin/users');
    const data = await res.json();
    setUsers(data.users || []);
    setLoading(false);
  }, []);

  useEffect(() => {
    // Async IIFE so the setState calls inside fetchUsers run after an await
    // (in a microtask), not synchronously in the effect body.
    void (async () => {
      await fetchUsers();
    })();
  }, [fetchUsers]);

  const approveUser = async (username: string) => {
    await fetch('/api/admin/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, action: 'approve' })
    });
    fetchUsers();
  };

  const denyUser = async (username: string) => {
    if (confirm(`Delete user ${username}?`)) {
      await fetch('/api/admin/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, action: 'deny' })
      });
      fetchUsers();
    }
  };

  if (loading) {
    return (
      <>
        <Head>
          <title>User Management - Game Agent</title>
        </Head>
        <div className="ga-layout">
          <Navigation pageTitle="User Management" showBackButton={true} />
          <div style={{ padding: '40px', color: 'white', textAlign: 'center' }}>
            <div style={{ fontSize: '18px' }}>Loading users...</div>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <Head>
        <title>User Management - Game Agent</title>
      </Head>
      <div className="ga-layout">
        <Navigation pageTitle="User Management" showBackButton={true} />

        <main className="ga-admin-main">
          <div className="ga-admin-container">
            <div className="ga-admin-header">
              <h2>User Management</h2>
              <p>Manage user access and permissions for Game Agent</p>
            </div>

            <div className="ga-users-table-container">
              <table className="ga-users-table">
                <thead>
                  <tr>
                    <th>Email</th>
                    <th>Status</th>
                    <th>Groups</th>
                    <th>Created</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map(user => (
                    <tr key={user.username}>
                      <td>{user.email}</td>
                      <td>
                        <span className={`ga-status-badge ${user.status.toLowerCase()}`}>
                          {user.status}
                        </span>
                      </td>
                      <td>{user.groups.join(', ') || 'None'}</td>
                      <td>{new Date(user.created).toLocaleDateString()}</td>
                      <td>
                        {user.status === 'UNCONFIRMED' && !user.groups.includes('admin') && (
                          <div className="ga-action-buttons">
                            <button
                              onClick={() => approveUser(user.username)}
                              className="ga-approve-button"
                            >
                              Approve
                            </button>
                            <button
                              onClick={() => denyUser(user.username)}
                              className="ga-deny-button"
                            >
                              Deny
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </main>
      </div>

      <style jsx>{`
        .ga-layout {
          min-height: 100vh;
          background: linear-gradient(135deg, #0f0f23 0%, #1a1a2e 50%, #16213e 100%);
          color: white;
        }

        .ga-admin-main {
          padding: 40px 0;
        }

        .ga-admin-container {
          max-width: 1200px;
          margin: 0 auto;
          padding: 0 24px;
        }

        .ga-admin-header {
          margin-bottom: 32px;
        }

        .ga-admin-header h2 {
          margin: 0 0 8px 0;
          font-size: 2rem;
          font-weight: 700;
          background: linear-gradient(135deg, #8b45ff 0%, #3d5afe 50%, #00bcd4 100%);
          -webkit-background-clip: text;
          -webkit-text-fill-color: transparent;
          background-clip: text;
        }

        .ga-admin-header p {
          margin: 0;
          color: rgba(255, 255, 255, 0.7);
          font-size: 1rem;
        }

        .ga-users-table-container {
          background: rgba(26, 26, 46, 0.6);
          border: 1px solid rgba(139, 69, 255, 0.3);
          border-radius: 12px;
          padding: 24px;
          backdrop-filter: blur(10px);
        }

        .ga-users-table {
          width: 100%;
          border-collapse: collapse;
        }

        .ga-users-table th {
          padding: 16px 12px;
          text-align: left;
          font-weight: 600;
          color: rgba(255, 255, 255, 0.9);
          border-bottom: 2px solid rgba(139, 69, 255, 0.4);
          font-size: 14px;
        }

        .ga-users-table td {
          padding: 16px 12px;
          border-bottom: 1px solid rgba(139, 69, 255, 0.2);
          font-size: 14px;
        }

        .ga-status-badge {
          padding: 4px 12px;
          border-radius: 16px;
          font-size: 12px;
          font-weight: 500;
          text-transform: uppercase;
        }

        .ga-status-badge.confirmed {
          background: rgba(0, 255, 136, 0.2);
          color: #00ff88;
          border: 1px solid rgba(0, 255, 136, 0.3);
        }

        .ga-status-badge.unconfirmed {
          background: rgba(255, 165, 0, 0.2);
          color: #ffa500;
          border: 1px solid rgba(255, 165, 0, 0.3);
        }

        .ga-action-buttons {
          display: flex;
          gap: 8px;
        }

        .ga-approve-button {
          padding: 6px 12px;
          background: rgba(0, 255, 136, 0.2);
          border: 1px solid rgba(0, 255, 136, 0.3);
          border-radius: 6px;
          color: #00ff88;
          font-size: 12px;
          font-weight: 500;
          cursor: pointer;
          transition: all 0.2s;
        }

        .ga-approve-button:hover {
          background: rgba(0, 255, 136, 0.3);
          border-color: rgba(0, 255, 136, 0.5);
        }

        .ga-deny-button {
          padding: 6px 12px;
          background: rgba(255, 69, 69, 0.2);
          border: 1px solid rgba(255, 69, 69, 0.3);
          border-radius: 6px;
          color: #ff6b6b;
          font-size: 12px;
          font-weight: 500;
          cursor: pointer;
          transition: all 0.2s;
        }

        .ga-deny-button:hover {
          background: rgba(255, 69, 69, 0.3);
          border-color: rgba(255, 69, 69, 0.5);
        }
      `}</style>
    </>
  );
}
