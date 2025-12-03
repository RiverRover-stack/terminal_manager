# The Role of Graphic Walker

Graphic Walker is a critical component of this project, serving as the "Self-Service" counterpart to the "AI-Generated" dashboard. It provides a lightweight, Tableau-like interface embedded directly in the browser.

## Why is it Important?

While the AI (Ollama + Streamlit) is excellent for generating fixed, polished dashboards based on a specific question, it is a "Black Box" — the user gets what the AI thinks they want.

**Graphic Walker fills the gap for Exploratory Data Analysis (EDA):**

1.  **True Self-Service**: Users can drag and drop fields to create their own charts without writing a single prompt or line of code.
2.  **Verification**: Users can verify the AI's findings by manually exploring the data.
3.  **No Hallucinations**: Unlike LLMs, Graphic Walker plots exactly the data present in the file, providing a ground-truth view.
4.  **Privacy**: It runs entirely in the browser (client-side), making it fast and secure for sensitive data.

## Implementation Details

*   **Frontend Library**: The project uses `@kanaries/graphic-walker` in the React frontend.
*   **Integration**: It is wrapped in the `SimpleChart.js` and `StandaloneChart.js` components.
*   **Workflow**:
    1.  When a user uploads a file, the system parses it.
    2.  If the user asks a question, the AI generates a Streamlit dashboard.
    3.  If the user wants to *explore*, Graphic Walker is rendered, allowing them to drag fields like `GrossQuantity` onto axes to discover insights manually.
*   **Backend Support**: The .NET Backend (`WebApplication1`) includes a `GraphicWalkerController` designed to save and load these manual dashboard configurations, allowing users to persist their work.

## How to Explain it to an Interviewer

> "In this project, I used a hybrid approach. I use Generative AI (Ollama) to instantly answer specific business questions with Python-generated dashboards. However, I also integrated **Graphic Walker** to provide a robust, Tableau-style interface for manual data exploration. This ensures that while executives get their AI answers, analysts still have the granular control they need to dig into the data themselves, all within the same web application."
