/**
 * End-to-End Performance Tests for Game Agent
 *
 * Tests the complete user journey from UI interaction to backend response,
 * measuring real-world performance from a user perspective.
 * Tags: @e2e @performance @slow
 */

import { test, expect, Page } from '@playwright/test';

interface PerformanceMetrics {
  requestStart: number;
  responseEnd: number;
  totalTime: number;
  query: string;
  responseLength: number;
}

class GameAgentPerformanceTester {
  private page: Page;
  private metrics: PerformanceMetrics[] = [];

  constructor(page: Page) {
    this.page = page;
  }

  async measureChatResponse(query: string): Promise<PerformanceMetrics> {
    const startTime = Date.now();

    // Find chat input and send message
    const chatInput = this.page.locator('input[type="text"], textarea').first();
    await chatInput.fill(query);

    // Click send button or press Enter
    const sendButton = this.page.locator('button').filter({ hasText: /send|submit/i }).first();

    let responseReceived = false;
    let responseLength = 0;

    // Listen for response using correct CopilotKit selectors
    const responsePromise = this.page.waitForFunction(() => {
      // Look for CopilotKit assistant messages
      const messages = document.querySelectorAll('.copilotKitAssistantMessage');
      return messages.length > 0;
    }, { timeout: 60000 }); // 60s timeout

    // Send the message
    if (await sendButton.isVisible()) {
      await sendButton.click();
    } else {
      await chatInput.press('Enter');
    }

    // Wait for response
    await responsePromise;

    const endTime = Date.now();
    const totalTime = endTime - startTime;

    // Get response length using correct CopilotKit selector
    const lastMessage = await this.page.locator('.copilotKitAssistantMessage').last().textContent();
    responseLength = lastMessage?.length || 0;

    const metrics: PerformanceMetrics = {
      requestStart: startTime,
      responseEnd: endTime,
      totalTime,
      query,
      responseLength
    };

    this.metrics.push(metrics);
    return metrics;
  }

  getMetrics(): PerformanceMetrics[] {
    return this.metrics;
  }

  getAverageResponseTime(): number {
    if (this.metrics.length === 0) return 0;
    return this.metrics.reduce((sum, m) => sum + m.totalTime, 0) / this.metrics.length;
  }

  logPerformanceReport(): void {
    console.log('\n📊 PERFORMANCE REPORT');
    console.log('=' .repeat(50));

    this.metrics.forEach((metric, index) => {
      console.log(`\n${index + 1}. Query: "${metric.query}"`);
      console.log(`   Response Time: ${metric.totalTime}ms`);
      console.log(`   Response Length: ${metric.responseLength} chars`);
      console.log(`   Performance: ${metric.totalTime < 10000 ? '✅ Good' : metric.totalTime < 20000 ? '⚠️ Slow' : '❌ Too Slow'}`);
    });

    console.log(`\n📈 Average Response Time: ${this.getAverageResponseTime().toFixed(0)}ms`);
    console.log(`📊 Total Tests: ${this.metrics.length}`);

    const fastTests = this.metrics.filter(m => m.totalTime < 10000).length;
    const slowTests = this.metrics.filter(m => m.totalTime >= 10000 && m.totalTime < 20000).length;
    const verySlowTests = this.metrics.filter(m => m.totalTime >= 20000).length;

    console.log(`✅ Fast (<10s): ${fastTests}`);
    console.log(`⚠️ Slow (10-20s): ${slowTests}`);
    console.log(`❌ Very Slow (>20s): ${verySlowTests}`);
  }
}

test.describe('Game Agent E2E Performance Tests', { tag: ['@e2e', '@performance', '@slow'] }, () => {
  let tester: GameAgentPerformanceTester;

  test.beforeEach(async ({ page }) => {
    tester = new GameAgentPerformanceTester(page);

    // Navigate to the application
    await page.goto('http://localhost:3000');

    // Wait for the chat interface to load
    await page.waitForSelector('input[type="text"], textarea', { timeout: 10000 });

    // Wait a bit more for full initialization
    await page.waitForTimeout(2000);
  });

  test('should respond to simple queries quickly', async ({ page }) => {
    const metrics = await tester.measureChatResponse('hello');

    expect(metrics.totalTime).toBeLessThan(15000); // 15s max for simple queries
    expect(metrics.responseLength).toBeGreaterThan(10); // Should get a real response
  });

  test('should handle EKS cluster listing efficiently', async ({ page }) => {
    const metrics = await tester.measureChatResponse('list my EKS clusters');

    expect(metrics.totalTime).toBeLessThan(25000); // 25s max for EKS queries
    expect(metrics.responseLength).toBeGreaterThan(50); // Should get detailed response
  });

  test('should handle GameLift queries efficiently', async ({ page }) => {
    const metrics = await tester.measureChatResponse('show me my GameLift fleets');

    expect(metrics.totalTime).toBeLessThan(25000); // 25s max for GameLift queries
    expect(metrics.responseLength).toBeGreaterThan(50); // Should get detailed response
  });

  test('should handle cost analysis queries', async ({ page }) => {
    const metrics = await tester.measureChatResponse('what is my AWS spending?');

    expect(metrics.totalTime).toBeLessThan(30000); // 30s max for cost queries
    expect(metrics.responseLength).toBeGreaterThan(30); // Should get some response
  });

  test.afterEach(async ({ page }) => {
    // Log performance metrics after each test
    tester.logPerformanceReport();
  });
});

test.describe('Performance Regression Tests', { tag: ['@e2e', '@performance', '@slow'] }, () => {
  test('should meet performance benchmarks', async ({ page }) => {
    const tester = new GameAgentPerformanceTester(page);

    await page.goto('http://localhost:3000');
    await page.waitForSelector('input[type="text"], textarea', { timeout: 10000 });
    await page.waitForTimeout(2000);

    // Benchmark tests with specific performance targets
    const benchmarks = [
      { query: 'hello', maxTime: 5000, description: 'Simple greeting' },
      { query: 'list clusters', maxTime: 15000, description: 'EKS cluster listing' },
      { query: 'show fleets', maxTime: 15000, description: 'GameLift fleet listing' },
    ];

    let passedBenchmarks = 0;

    for (const benchmark of benchmarks) {
      const metrics = await tester.measureChatResponse(benchmark.query);

      if (metrics.totalTime <= benchmark.maxTime) {
        passedBenchmarks++;
        console.log(`✅ ${benchmark.description}: ${metrics.totalTime}ms (target: ${benchmark.maxTime}ms)`);
      } else {
        console.log(`❌ ${benchmark.description}: ${metrics.totalTime}ms (target: ${benchmark.maxTime}ms)`);
      }

      await page.waitForTimeout(1000);
    }

    // At least 80% of benchmarks should pass
    const passRate = passedBenchmarks / benchmarks.length;
    expect(passRate).toBeGreaterThanOrEqual(0.8);

    tester.logPerformanceReport();
  });
});
