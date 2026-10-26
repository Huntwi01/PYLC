import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'PYLC — Conveyor HMI',
  description:
    'Next.js operator panel for the PYLC soft-PLC conveyor demo. Talks to the demo_web API on port 8085.',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
