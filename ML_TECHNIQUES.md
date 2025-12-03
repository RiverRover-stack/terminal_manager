# Machine Learning Techniques in Terminal Manager Dashboard

## Overview

The functionality of the Terminal Manager Dashboard is powered by a combination of advanced **Generative AI** techniques and robust **Heuristic Algorithms**. This document details the specific machine learning concepts used and how they contribute to the system's "Text-to-Dashboard" capabilities.

## 1. Generative AI & Large Language Models (LLM)

The core intelligence of the system relies on **Ollama**, which runs large language models (specifically **Llama 3**) locally.

### Technique: Retrieval-Augmented Generation (RAG)
Instead of asking the LLM to write code in a vacuum, the system employs a specific flavor of RAG to ensure accuracy.

*   **How it works**:
    1.  **Retrieval**: When a user asks a question, the system first scans the uploaded Excel file (converted to SQLite). It "retrieves" the exact **database schema** (table names, column names like `GrossQuantity`, `FlowRate`) and extracts a **sample of real data** (the first 5 rows).
    2.  **Augmentation**: It dynamically constructs a prompt that combines:
        *   The User's Request (e.g., "Show me OEE by lane").
        *   The Retrieved Schema.
        *   The Sample Data.
        *   Strict coding constraints (e.g., "Use Plotly", "Do not hallucinate data").
    3.  **Generation**: The LLM generates executable Python code (`streamlit` script) that is guaranteed to work with the specific data file provided.

*   **Contribution**: This allows the dashboard to adapt to *any* Excel file structure without retraining. It grounds the AI in reality, preventing it from inventing columns that don't exist.

## 2. Natural Language Processing (NLP)

### Intent Classification (Heuristic & LLM)
The system uses NLP to understand *what* the user wants to build.

*   **Rule-Based Keyword Matching**: In `ai-backend/app.py`, the `analyze_dashboard_type` function scans the user's prompt for domain-specific keywords.
    *   *Keywords*: "throughput", "lane", "bay" -> **Manufacturing Dashboard**
    *   *Keywords*: "revenue", "profit" -> **Financial Dashboard**
    *   *Keywords*: "fuel", "energy" -> **Energy Dashboard**
*   **Semantic Understanding**: The LLM handles the nuance. For example, if a user asks for "efficiency", the LLM infers this implies calculating `Throughput / Capacity` or `OEE` based on the available columns, even if the user didn't explicitly ask for that formula.

*   **Contribution**: This automatically selects the correct visual template (e.g., selecting "Gauge Charts" for operational metrics vs. "Funnel Charts" for sales), providing a professional starting point.

## 3. Heuristic Data Analysis

While not "Deep Learning," the system employs sophisticated heuristic algorithms in `simple-backend/server.js` to automate data understanding, often referred to as "Lightweight ML."

### Automated Field Inference
*   **Technique**: The `analyzeFieldTypes` function scans data columns to statistically determine their semantic type.
    *   *Logic*: If >70% of values are parseable numbers -> **Measure** (Quantitative).
    *   *Logic*: If values parse as dates -> **Temporal**.
    *   *Logic*: Otherwise -> **Dimension** (Nominal).
*   **Contribution**: This powers the **Graphic Walker** interface, automatically setting up axes so users can drag-and-drop fields without needing to define data types manually.

### Fuzzy Field Matching
*   **Technique**: The `matchFieldToPrompt` function uses a synonym dictionary (`createFieldSynonyms`) and partial string matching to map user terms to database columns.
    *   *Example*: User types "Sales" -> System matches it to column `GrossRevenue` or `TotalAmount`.
*   **Contribution**: This bridge allows users to speak in business terms ("Show me sales") rather than database terms ("Show me column 'Gross_Amt_USD'"), significantly improving the User Experience (UX).

## Summary Table

| Component | Technique | Role |
| :--- | :--- | :--- |
| **Code Generation** | **LLM (Llama 3)** | Writes the Python/Streamlit code to build the dashboard. |
| **Context Accuracy** | **RAG** | Feeds real schema/data into the LLM to prevent errors. |
| **Intent Detection** | **NLP (Keywords)** | Determines if the dashboard is for Manufacturing, Sales, etc. |
| **Data Auto-Setup** | **Heuristics** | Infers if a column is a Number, Date, or Category. |
| **UX Mapping** | **Fuzzy Logic** | Maps user words ("Money") to data columns ("Revenue"). |
