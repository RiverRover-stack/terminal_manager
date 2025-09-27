from flask import Flask, request, jsonify
from flask_cors import CORS
import pandas as pd
import sqlite3
import os
import subprocess
import time
import json
import threading
from datetime import datetime
import signal
import requests
from dotenv import load_dotenv
from config import Config

load_dotenv()

# quick setup for demo
app = Flask(__name__)
CORS(app, origins=Config.CORS_ORIGINS)
Config.ensure_directories()

# TODO: maybe use a proper database later
running_dashboards = {}

class DashboardGenerator:
    def __init__(self):
        self.ollama_url = f"{Config.OLLAMA_URL}/api/generate"
        self.excel_dir = os.path.join(Config.PROJECT_ROOT, 'excel-data')
        self.ensure_excel_directory()  # make sure folder exists

    def sanitize_table_name(self, name):
        import re
        # clean up the filename for sqlite
        sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', str(name).lower())
        sanitized = re.sub(r'_+', '_', sanitized)
        if sanitized and sanitized[0].isdigit():
            sanitized = 'data_' + sanitized  # sqlite doesn't like numbers first
        if not sanitized or len(sanitized.strip('_')) == 0:
            sanitized = 'data_table'  # fallback name
        if len(sanitized) > 50:
            sanitized = sanitized[:50]  # keep it short
        sanitized = sanitized.rstrip('_')
        if not sanitized:
            sanitized = 'data_table'  # final fallback
        return sanitized

    def ensure_excel_directory(self):
        os.makedirs(self.excel_dir, exist_ok=True)

    def find_excel_files(self):
        if not os.path.exists(self.excel_dir):
            return []
        excel_files = []
        for filename in os.listdir(self.excel_dir):
            if filename.lower().endswith(('.xlsx', '.xls')) and not filename.startswith('~'):
                excel_files.append(os.path.join(self.excel_dir, filename))
        return sorted(excel_files, key=os.path.getmtime, reverse=True)

    def get_foolproof_table_name(self, index):
        return f"dataset_{index + 1:03d}"

    def process_all_excel_files(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(Config.DATA_DIR, 'evaluation_data.db')
        excel_files = self.find_excel_files()
        if not excel_files:
            return {'success': False, 'error': 'No Excel files found in excel-data directory'}
        latest_file = excel_files[0]
        try:
            df = pd.read_excel(latest_file)
            if df.empty:
                return {'success': False, 'error': 'Excel file is empty'}
            processed_df = self.process_shipment_data(df)
            conn = sqlite3.connect(db_path)
            table_name = "evaluation_data"
            processed_df.to_sql(table_name, conn, if_exists='replace', index=False)
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info([{table_name}])")
            columns = cursor.fetchall()
            conn.close()

            return {
                'success': True,
                'db_path': db_path,
                'table_name': table_name,
                'columns': [col[1] for col in columns],
                'sample_data': processed_df.head(5).to_dict('records'),
                'data_type': 'shipment_logistics',
                'total_rows': len(processed_df),
                'excel_source': latest_file,
                'total_files_found': len(excel_files)
            }

        except Exception as e:
            return {'success': False, 'error': f'Failed to process Excel files: {str(e)}'}

    def convert_excel_to_sqlite(self, excel_path, db_path=None):
        if db_path is None:
            db_path = os.path.join(Config.DATA_DIR, 'terminal_data.db')
        try:
            df = pd.read_excel(excel_path, sheet_name=0, dtype=str)
            df.columns = [str(c).strip() for c in df.columns]
            processed_df = self.process_shipment_data(df)
            conn = sqlite3.connect(db_path)
            base_name = os.path.splitext(os.path.basename(excel_path))[0]
            table_name = self.sanitize_table_name(base_name)
            processed_df.to_sql(table_name, conn, if_exists='replace', index=False)
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info([{table_name}])")
            columns = cursor.fetchall()

            conn.close()

            return {
                'success': True,
                'db_path': db_path,
                'table_name': table_name,
                'columns': [col[1] for col in columns],
                'sample_data': processed_df.head(5).to_dict('records'),
                'data_type': 'shipment_logistics',
                'total_rows': len(processed_df),
                'excel_source': excel_path
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def process_shipment_data(self, df):
        try:
            processed_df = df.copy()
            # FIXME: this should be more generic
            numeric_columns = ['GrossQuantity', 'FlowRate']
            for col in numeric_columns:
                if col in processed_df.columns:
                    processed_df[col] = pd.to_numeric(processed_df[col], errors='coerce').fillna(0)  # convert to numbers, fill NaN with 0
            if 'ScheduledDate' in processed_df.columns:
                for date_format in ['%m-%d-%y', '%d-%m-%y', '%Y-%m-%d']:
                    try:
                        processed_df['ScheduledDate_parsed'] = pd.to_datetime(processed_df['ScheduledDate'], format=date_format)
                        break
                    except:
                        continue

            time_columns = ['ExitTime', 'CreatedTime']
            for col in time_columns:
                if col in processed_df.columns:
                    try:
                        processed_df[f'{col}_hour'] = pd.to_datetime(processed_df[col], format='%I:%M:%S %p', errors='coerce').dt.hour
                    except:
                        try:
                            processed_df[f'{col}_hour'] = pd.to_datetime(processed_df[col], format='%H:%M:%S', errors='coerce').dt.hour
                        except:
                            pass
            if 'BayCode' in processed_df.columns:
                processed_df['BayCode'] = processed_df['BayCode'].astype(str)
                processed_df['Lane'] = processed_df['BayCode'].str.extract(r'(LANE\d+)', expand=False).fillna(processed_df['BayCode'])
            if 'GrossQuantity' in processed_df.columns and 'FlowRate' in processed_df.columns:
                processed_df['Throughput_Units_Hour'] = processed_df['GrossQuantity'] * processed_df['FlowRate']
            if 'ExitTime_hour' in processed_df.columns:
                processed_df['Shift'] = processed_df['ExitTime_hour'].apply(
                    lambda x: 'Day_Shift' if pd.notna(x) and 6 <= x < 18 else 'Night_Shift' if pd.notna(x) else 'Unknown'
                )
            if 'ScheduledDate_parsed' in processed_df.columns:
                processed_df['Date'] = processed_df['ScheduledDate_parsed'].dt.date
                processed_df['Month'] = processed_df['ScheduledDate_parsed'].dt.to_period('M')

            return processed_df

        except Exception as e:
            print(f"Error processing shipment data: {e}")
            return df  # Return original if processing fails

    def call_llm(self, prompt):
        try:
            # basic ollama call - could be improved
            payload = {
                "model": Config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,  # not too creative
                    "top_p": 0.9
                }
            }
            print(f"Calling LLM with model: {Config.OLLAMA_MODEL}")
            response = requests.post(self.ollama_url, json=payload, timeout=Config.OLLAMA_TIMEOUT)

            if response.status_code == 200:
                result = response.json().get('response', '')
                if result.strip():
                    print("LLM generation successful")
                    return result
                else:
                    print("LLM returned empty response")
                    return None
            else:
                print(f"LLM API error: {response.status_code} - {response.text}")
                return None

        except requests.exceptions.Timeout:
            print(f"LLM timeout after {Config.OLLAMA_TIMEOUT} seconds")
            return None
        except requests.exceptions.ConnectionError:
            print("LLM connection error - Ollama may not be running")
            return None
        except Exception as e:
            print(f"LLM unexpected error: {e}")
            return None

    def analyze_dashboard_type(self, user_prompt):
        prompt_lower = user_prompt.lower()
        if any(word in prompt_lower for word in ['lane', 'bay', 'throughput', 'capacity', 'production', 'manufacturing', 'terminal', 'schedule', 'adherence']):
            return 'manufacturing'
        if any(word in prompt_lower for word in ['financial', 'revenue', 'profit', 'cost', 'budget', 'earnings', 'income', 'expense']):
            return 'financial'
        if any(word in prompt_lower for word in ['sales', 'revenue', 'customer', 'conversion', 'lead', 'pipeline', 'orders']):
            return 'sales'
        if any(word in prompt_lower for word in ['operational', 'efficiency', 'uptime', 'performance', 'productivity', 'operations']):
            return 'operational'
        if any(word in prompt_lower for word in ['logistics', 'supply', 'inventory', 'shipment', 'delivery', 'warehouse', 'freight']):
            return 'logistics'
        if any(word in prompt_lower for word in ['analytics', 'analysis', 'report', 'insights', 'trends', 'statistics']):
            return 'analytics'
        if any(word in prompt_lower for word in ['fuel', 'energy', 'consumption', 'volume', 'flow', 'rate']):
            return 'energy'
        if any(word in prompt_lower for word in ['employee', 'hr', 'staff', 'workforce', 'personnel', 'hiring']):
            return 'hr'
        return 'analytics'

    def get_dashboard_requirements(self, dashboard_type):
        requirements = {
            'manufacturing': """
- Focus on production metrics, lane/bay performance, throughput, capacity utilization, OEE
- Include real-time KPIs: production rates, downtime, quality metrics, schedule adherence
- Show performance by lane/bay, shift patterns, product mix analysis
- Color scheme: Industrial blue/steel gray with green for targets, red for alerts
- Charts: Real-time gauges, production trend lines, heatmaps for bay performance, Gantt charts for schedules
- Key metrics: Overall Equipment Effectiveness (OEE), First Pass Yield (FPY), Cycle Time, Takt Time
- Professional manufacturing aesthetic with clean, data-dense layouts""",

            'financial': """
- Focus on revenue, profit margins, cost analysis, ROI metrics
- Use currency formatting and financial KPIs
- Include trend analysis and variance reporting
- Color scheme: Green/red for profit/loss, blue for neutral metrics
- Charts: Line charts for trends, bar charts for comparisons, pie charts for breakdowns""",

            'sales': """
- Focus on sales volume, conversion rates, customer acquisition
- Include funnel analysis and performance tracking
- Show geographic and temporal sales patterns
- Color scheme: Blue/green sales theme
- Charts: Funnel charts, bar charts, geographic maps, time series""",

            'operational': """
- Focus on efficiency metrics, uptime, throughput, performance indicators
- Include KPI cards with status indicators
- Show operational trends and capacity utilization
- Color scheme: Blue/green for good performance, yellow/red for issues
- Charts: Gauge charts, line charts for trends, bar charts for comparisons""",

            'logistics': """
- Focus on shipment tracking, delivery performance, inventory levels
- Include route optimization and capacity planning
- Show supply chain metrics and bottlenecks
- Color scheme: Orange/blue logistics theme
- Charts: Geographic maps, flow charts, timeline charts, bar charts""",

            'analytics': """
- Focus on data exploration, trend analysis, statistical insights
- Include interactive filtering and drill-down capabilities
- Show correlations and data patterns
- Color scheme: Professional blue/gray theme
- Charts: Scatter plots, histograms, correlation matrices, trend lines""",

            'energy': """
- Focus on consumption patterns, efficiency metrics, volume analysis
- Include environmental and cost impact metrics
- Show usage trends and optimization opportunities
- Color scheme: Green/blue energy theme
- Charts: Area charts for consumption, gauge charts for efficiency, line charts for trends""",

            'hr': """
- Focus on employee metrics, performance, demographics, satisfaction
- Include workforce analytics and talent management
- Show hiring trends and retention analysis
- Color scheme: Purple/blue professional theme
- Charts: Bar charts for demographics, line charts for trends, pie charts for distributions"""
        }

        return requirements.get(dashboard_type, requirements['analytics'])

    def generate_dashboard_code(self, user_prompt, data_context):
        dashboard_type = self.analyze_dashboard_type(user_prompt)
        llm_prompt = f"""
You are an expert Python developer creating a Streamlit dashboard for shipment/logistics data analysis.

USER REQUEST: "{user_prompt}"
DASHBOARD TYPE: {dashboard_type}

ACTUAL DATA CONTEXT:
- Database: {data_context['db_path']}
- ⚠️  EXACT TABLE NAME TO USE: {data_context['table_name']} ⚠️
- Data Type: {data_context.get('data_type', 'shipment_logistics')}
- Total Rows: {data_context.get('total_rows', 'Unknown')}
- Available Columns: {', '.join(data_context['columns'])}
- Sample Data (first 5 rows): {json.dumps(data_context['sample_data'], indent=2)}
- Source file: {data_context.get('excel_source', 'Excel file')} (for reference only - do not use for table names)

IMPORTANT:
1. Use the ACTUAL data provided above. DO NOT generate sample data.
2. Load data from the SQLite database using EXACTLY this table name: {data_context['table_name']}
3. DO NOT derive table names from the Excel filename - use ONLY the table name provided above
4. Use this EXACT SQL query format: SELECT * FROM [{data_context['table_name']}]
5. Use the real columns and values shown in the sample data.

KEY DATA SCHEMA:
- BayCode/Lane: Use for lane/bay performance analysis
- GrossQuantity: Shipment quantities for throughput analysis
- FlowRate: Operational flow rates
- ScheduledDate: For schedule vs actual analysis
- ExitTime: For timing analysis and shift patterns
- BaseProductCode: For product mix analysis
- ShipmentID, ShipmentCompartmentID: For operational tracking

DASHBOARD TYPE SPECIFIC REQUIREMENTS:
{self.get_dashboard_requirements(dashboard_type)}

CRITICAL REQUIREMENTS:
1. Load data from SQLite database: sqlite3.connect('{data_context['db_path']}')
2. Query table: SELECT * FROM [{data_context['table_name']}]
3. Use ACTUAL column names from the data: {', '.join(data_context['columns'])}
4. Handle missing/null values gracefully
5. Create professional manufacturing-style dashboard
6. Focus on lane/bay performance, throughput, and operational metrics
7. Use Plotly for interactive charts
8. Include filtering and date range selection
9. NO sample data generation - use only real data from database

Generate ONLY the Python code for the Streamlit dashboard. Start directly with imports, no explanations.

```python
"""

        response = self.call_llm(llm_prompt)

        if response:
            # Extract Python code from response
            if '```python' in response:
                code = response.split('```python')[1].split('```')[0].strip()
            elif '```' in response:
                code = response.split('```')[1].strip()
            else:
                code = response.strip()

            return code

        # Fallback template if LLM fails
        return self.get_fallback_dashboard(data_context, dashboard_type)

    def get_fallback_dashboard(self, data_context, dashboard_type='operational'):
        table_name = data_context.get('table_name', 'data_table')
        columns = data_context.get('columns', [])
        if dashboard_type == 'manufacturing':
            return self.get_manufacturing_template(table_name, columns)
        elif dashboard_type == 'financial':
            return self.get_financial_template(table_name, columns)
        elif dashboard_type == 'sales':
            return self.get_sales_template(table_name, columns)
        elif dashboard_type == 'logistics':
            return self.get_logistics_template(table_name, columns)
        elif dashboard_type == 'analytics':
            return self.get_analytics_template(table_name, columns)
        elif dashboard_type == 'energy':
            return self.get_energy_template(table_name, columns)
        elif dashboard_type == 'hr':
            return self.get_hr_template(table_name, columns)
        else:
            return self.get_operational_template(table_name, columns)

    def get_manufacturing_template(self, table_name, columns):
        return fr"""
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import sqlite3
import numpy as np
from datetime import datetime, timedelta
import plotly.figure_factory as ff

# Page configuration for manufacturing dashboard
st.set_page_config(
    page_title="Manufacturing Performance Dashboard",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Professional manufacturing CSS styling
st.markdown('''
<style>
    .main {{
        background: linear-gradient(135deg, #1a202c 0%, #2d3748 100%);
        color: white;
        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    }}
    .metric-card {{
        background: linear-gradient(135deg, #ffffff 0%, #f7fafc 100%);
        color: #1a202c;
        padding: 2rem;
        border-radius: 16px;
        box-shadow: 0 10px 40px rgba(0,0,0,0.15);
        margin: 0.75rem 0;
        border-left: 6px solid #3182ce;
        transition: transform 0.2s ease;
    }}
    .metric-card:hover {{
        transform: translateY(-2px);
        box-shadow: 0 15px 50px rgba(0,0,0,0.2);
    }}
    .kpi-header {{
        font-size: 0.95rem;
        font-weight: 700;
        color: #4a5568;
        margin-bottom: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}
    .kpi-value {{
        font-size: 3rem;
        font-weight: 900;
        color: #1a202c;
        margin: 0;
        line-height: 1;
    }}
    .kpi-delta {{
        font-size: 0.9rem;
        margin-top: 0.5rem;
        font-weight: 600;
    }}
    .kpi-unit {{
        font-size: 1.2rem;
        color: #718096;
        margin-left: 0.5rem;
    }}
    .status-excellent {{ color: #38a169; }}
    .status-good {{ color: #4299e1; }}
    .status-warning {{ color: #ed8936; }}
    .status-critical {{ color: #e53e3e; }}
    .section-header {{
        color: white;
        font-size: 1.8rem;
        font-weight: 700;
        margin: 2rem 0 1rem 0;
        padding-bottom: 0.5rem;
        border-bottom: 3px solid #3182ce;
    }}
    .stPlotlyChart {{
        background: rgba(255,255,255,0.98);
        border-radius: 16px;
        padding: 1.5rem;
        box-shadow: 0 8px 32px rgba(0,0,0,0.12);
        margin: 1rem 0;
    }}
    .alert-banner {{
        background: linear-gradient(90deg, #fed7d7 0%, #feb2b2 100%);
        color: #c53030;
        padding: 1rem;
        border-radius: 12px;
        margin: 1rem 0;
        font-weight: 600;
        border-left: 6px solid #e53e3e;
    }}
    .target-banner {{
        background: linear-gradient(90deg, #c6f6d5 0%, #9ae6b4 100%);
        color: #2f855a;
        padding: 1rem;
        border-radius: 12px;
        margin: 1rem 0;
        font-weight: 600;
        border-left: 6px solid #38a169;
    }}
</style>
''', unsafe_allow_html=True)

# Dashboard header
st.markdown("<h1 style='text-align: center; color: white; margin-bottom: 1rem; font-size: 3rem;'>🏭 Manufacturing Performance Dashboard</h1>", unsafe_allow_html=True)
st.markdown("<h3 style='text-align: center; color: #a0aec0; margin-bottom: 3rem;'>Real-time Production Metrics • Lane/Bay Performance • OEE Analytics</h3>", unsafe_allow_html=True)

# Load and process data
@st.cache_data
def load_data():
    try:
        # Load actual data from SQLite database
        conn = sqlite3.connect('{data_context["db_path"]}')
        df = pd.read_sql_query(f"SELECT * FROM [{data_context['table_name']}]", conn)
        conn.close()

        # Process the data for dashboard use
        if not df.empty:
            # Convert numeric columns
            numeric_cols = ['GrossQuantity', 'FlowRate', 'Throughput_Units_Hour']
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

            # Process date/time columns
            if 'ScheduledDate_parsed' in df.columns:
                df['ScheduledDate_parsed'] = pd.to_datetime(df['ScheduledDate_parsed'], errors='coerce')
                df['Date'] = df['ScheduledDate_parsed'].dt.date

            # Handle missing lane/bay data
            if 'Lane' not in df.columns and 'BayCode' in df.columns:
                df['Lane'] = df['BayCode'].str.extract(r'(LANE\d+)', expand=False).fillna(df['BayCode'])

            st.success(f"Loaded {{len(df)}} records from {{'{data_context['excel_source']}'}} via database")
            return df
        else:
            st.warning("No data found in database")
            return pd.DataFrame()

    except Exception as e:
        st.error(f"Error loading data: {{str(e)}}")
        return pd.DataFrame()

@st.cache_data
def generate_sample_manufacturing_data():
    \"\"\"Generate realistic manufacturing sample data\"\"\"
    try:
        np.random.seed(42)
        dates = pd.date_range(start=datetime.now() - timedelta(days=7), end=datetime.now(), freq='H')

    lanes = ['Lane_A', 'Lane_B', 'Lane_C', 'Lane_D']
    bays = ['Bay_1', 'Bay_2', 'Bay_3', 'Bay_4', 'Bay_5']
    products = ['Product_X', 'Product_Y', 'Product_Z']
    shifts = ['Day_Shift', 'Night_Shift']

    data = []
    for i, timestamp in enumerate(dates):
        # Simulate realistic production patterns
        hour = timestamp.hour
        is_peak = 8 <= hour <= 16  # Day shift peak hours

        for lane in lanes:
            for bay in bays[:3]:  # 3 bays per lane
                base_throughput = 100 if is_peak else 60
                throughput = max(0, base_throughput + np.random.normal(0, 15))

                # Calculate OEE components
                availability = max(0.7, min(0.99, 0.85 + np.random.normal(0, 0.05)))
                performance = max(0.6, min(0.98, 0.82 + np.random.normal(0, 0.08)))
                quality = max(0.8, min(0.99, 0.94 + np.random.normal(0, 0.03)))
                oee = availability * performance * quality

                # Schedule adherence
                schedule_adherence = max(0.7, min(1.0, 0.88 + np.random.normal(0, 0.06)))

                data.append({{
                    'Timestamp': timestamp,
                    'Date': timestamp.date(),
                    'Hour': hour,
                    'Lane': lane,
                    'Bay': bay,
                    'Product': np.random.choice(products),
                    'Shift': 'Day_Shift' if 6 <= hour < 18 else 'Night_Shift',
                    'Planned_Production': int(110 + np.random.normal(0, 10)),
                    'Actual_Production': int(throughput),
                    'Throughput_Units_Hour': int(throughput),
                    'OEE_Percentage': round(oee * 100, 1),
                    'Availability': round(availability * 100, 1),
                    'Performance': round(performance * 100, 1),
                    'Quality': round(quality * 100, 1),
                    'Schedule_Adherence': round(schedule_adherence * 100, 1),
                    'Downtime_Minutes': max(0, int(np.random.exponential(5))),
                    'Cycle_Time_Seconds': round(60 + np.random.normal(0, 8), 1),
                    'Energy_Consumption_kWh': round(50 + np.random.normal(0, 8), 1),
                    'Temperature_C': round(22 + np.random.normal(0, 2), 1),
                    'Operator': f'OP_{{np.random.randint(100, 999)}}',
                    'Quality_Score': round(quality * 100, 1)
                }})

        return pd.DataFrame(data)

    except Exception as e:
        # If sample data generation fails, return minimal sample data
        st.error(f"Error generating sample data: {{str(e)}}")
        return pd.DataFrame({{
            'Timestamp': [datetime.now()],
            'Lane': ['Lane_A'],
            'Bay': ['Bay_1'],
            'OEE_Percentage': [85.0],
            'Throughput_Units_Hour': [100],
            'Schedule_Adherence': [95.0],
            'Quality_Score': [98.0]
        }})

# Load actual data
df = load_data()

# Ensure data is available
if df.empty:
    st.error("No data available for dashboard. Please check your Excel file upload.")
    st.stop()

# Process data for dashboard
if 'Date' in df.columns:
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')

# Sidebar controls
st.sidebar.markdown("## 📊 Dashboard Controls")
if 'Date' in df.columns:
    date_range = st.sidebar.date_input(
        "Select Date Range",
        value=[df['Date'].min().date(), df['Date'].max().date()],
        min_value=df['Date'].min().date(),
        max_value=df['Date'].max().date()
    )
else:
    date_range = [datetime.now().date() - timedelta(days=7), datetime.now().date()]

# Filter controls
lanes = st.sidebar.multiselect("Select Lanes",
                              options=df['Lane'].unique() if 'Lane' in df.columns else ['All'],
                              default=df['Lane'].unique() if 'Lane' in df.columns else ['All'])
shifts = st.sidebar.multiselect("Select Shifts",
                               options=df['Shift'].unique() if 'Shift' in df.columns else ['All'],
                               default=df['Shift'].unique() if 'Shift' in df.columns else ['All'])

# Apply filters
filtered_df = df.copy()
if 'Date' in df.columns and len(date_range) == 2:
    filtered_df = filtered_df[
        (filtered_df['Date'] >= pd.to_datetime(date_range[0])) &
        (filtered_df['Date'] <= pd.to_datetime(date_range[1]))
    ]
if lanes and 'Lane' in df.columns:
    filtered_df = filtered_df[filtered_df['Lane'].isin(lanes)]
if shifts and 'Shift' in df.columns:
    filtered_df = filtered_df[filtered_df['Shift'].isin(shifts)]

# Key Performance Indicators
st.markdown('<div class="section-header">🎯 Key Performance Indicators</div>', unsafe_allow_html=True)

if not filtered_df.empty:
    kpi_cols = st.columns(6)

    # Overall Equipment Effectiveness (OEE)
    avg_oee = filtered_df['OEE_Percentage'].mean() if 'OEE_Percentage' in filtered_df.columns else 0
    oee_status = "status-excellent" if avg_oee >= 85 else "status-good" if avg_oee >= 75 else "status-warning" if avg_oee >= 60 else "status-critical"

    with kpi_cols[0]:
        st.markdown(f'''
        <div class="metric-card">
            <div class="kpi-header">Overall Equipment Effectiveness</div>
            <div class="kpi-value {{oee_status}}">{{avg_oee:.1f}}<span class="kpi-unit">%</span></div>
            <div class="kpi-delta">Target: 85%+ | World Class: 90%+</div>
        </div>
        ''', unsafe_allow_html=True)

    # Total Throughput
    total_throughput = filtered_df['Throughput_Units_Hour'].sum() if 'Throughput_Units_Hour' in filtered_df.columns else 0
    with kpi_cols[1]:
        st.markdown(f'''
        <div class="metric-card">
            <div class="kpi-header">Total Throughput</div>
            <div class="kpi-value">{{total_throughput:,.0f}}<span class="kpi-unit">units</span></div>
            <div class="kpi-delta">Last {{len(filtered_df)}} data points</div>
        </div>
        ''', unsafe_allow_html=True)

    # Schedule Adherence
    avg_adherence = filtered_df['Schedule_Adherence'].mean() if 'Schedule_Adherence' in filtered_df.columns else 0
    adherence_status = "status-excellent" if avg_adherence >= 95 else "status-good" if avg_adherence >= 90 else "status-warning"
    with kpi_cols[2]:
        st.markdown(f'''
        <div class="metric-card">
            <div class="kpi-header">Schedule Adherence</div>
            <div class="kpi-value {{adherence_status}}">{{avg_adherence:.1f}}<span class="kpi-unit">%</span></div>
            <div class="kpi-delta">Target: 95%+</div>
        </div>
        ''', unsafe_allow_html=True)

    # Quality Score
    avg_quality = filtered_df['Quality_Score'].mean() if 'Quality_Score' in filtered_df.columns else 0
    quality_status = "status-excellent" if avg_quality >= 98 else "status-good" if avg_quality >= 95 else "status-warning"
    with kpi_cols[3]:
        st.markdown(f'''
        <div class="metric-card">
            <div class="kpi-header">Quality Score</div>
            <div class="kpi-value {{quality_status}}">{{avg_quality:.1f}}<span class="kpi-unit">%</span></div>
            <div class="kpi-delta">First Pass Yield Target: 98%</div>
        </div>
        ''', unsafe_allow_html=True)

    # Average Cycle Time
    avg_cycle_time = filtered_df['Cycle_Time_Seconds'].mean() if 'Cycle_Time_Seconds' in filtered_df.columns else 0
    with kpi_cols[4]:
        st.markdown(f'''
        <div class="metric-card">
            <div class="kpi-header">Avg Cycle Time</div>
            <div class="kpi-value">{{avg_cycle_time:.1f}}<span class="kpi-unit">sec</span></div>
            <div class="kpi-delta">Takt Time Optimization</div>
        </div>
        ''', unsafe_allow_html=True)

    # Total Downtime
    total_downtime = filtered_df['Downtime_Minutes'].sum() if 'Downtime_Minutes' in filtered_df.columns else 0
    with kpi_cols[5]:
        st.markdown(f'''
        <div class="metric-card">
            <div class="kpi-header">Total Downtime</div>
            <div class="kpi-value status-warning">{{total_downtime:,.0f}}<span class="kpi-unit">min</span></div>
            <div class="kpi-delta">Minimize unplanned stops</div>
        </div>
        ''', unsafe_allow_html=True)

# Performance alerts
if avg_oee < 75:
    st.markdown('<div class="alert-banner">⚠️ Alert: OEE below target threshold. Review availability, performance, and quality metrics.</div>', unsafe_allow_html=True)
elif avg_oee >= 85:
    st.markdown('<div class="target-banner">✅ Excellent: OEE exceeds target. Maintain current operational excellence.</div>', unsafe_allow_html=True)

# Production Analysis Charts
st.markdown('<div class="section-header">📈 Production Performance Analysis</div>', unsafe_allow_html=True)

chart_col1, chart_col2 = st.columns(2)

with chart_col1:
    # Throughput by Lane/Bay
    if 'Lane' in filtered_df.columns and 'Bay' in filtered_df.columns and 'Throughput_Units_Hour' in filtered_df.columns:
        bay_performance = filtered_df.groupby(['Lane', 'Bay'])['Throughput_Units_Hour'].sum().reset_index()
        bay_performance['Lane_Bay'] = bay_performance['Lane'] + ' - ' + bay_performance['Bay']

        fig_throughput = px.bar(bay_performance,
                               x='Lane_Bay',
                               y='Throughput_Units_Hour',
                               color='Throughput_Units_Hour',
                               color_continuous_scale='Blues',
                               title="🏭 Throughput by Lane/Bay",
                               labels={{'Throughput_Units_Hour': 'Total Units', 'Lane_Bay': 'Lane - Bay'}})
        fig_throughput.update_layout(height=400, showlegend=False, xaxis_tickangle=-45)
        st.plotly_chart(fig_throughput, use_container_width=True)

with chart_col2:
    # OEE Trend Analysis
    if 'Timestamp' in filtered_df.columns and 'OEE_Percentage' in filtered_df.columns:
        hourly_oee = filtered_df.groupby(filtered_df['Timestamp'].dt.floor('H'))['OEE_Percentage'].mean().reset_index()

        fig_oee = go.Figure()
        fig_oee.add_trace(go.Scatter(x=hourly_oee['Timestamp'],
                                   y=hourly_oee['OEE_Percentage'],
                                   mode='lines+markers',
                                   name='OEE %',
                                   line=dict(color='#3182ce', width=3),
                                   marker=dict(size=6)))
        fig_oee.add_hline(y=85, line_dash="dash", line_color="#38a169",
                         annotation_text="Target: 85%")
        fig_oee.add_hline(y=90, line_dash="dash", line_color="#2f855a",
                         annotation_text="World Class: 90%")
        fig_oee.update_layout(title="📊 OEE Trend Analysis",
                            height=400,
                            showlegend=False,
                            yaxis_title="OEE (%)")
        st.plotly_chart(fig_oee, use_container_width=True)

# Advanced Analytics Section
st.markdown('<div class="section-header">🔬 Advanced Manufacturing Analytics</div>', unsafe_allow_html=True)

analysis_col1, analysis_col2, analysis_col3 = st.columns(3)

with analysis_col1:
    # Schedule vs Actual Performance
    if 'Planned_Production' in filtered_df.columns and 'Actual_Production' in filtered_df.columns:
        schedule_data = filtered_df.groupby('Lane').agg({{
            'Planned_Production': 'sum',
            'Actual_Production': 'sum'
        }}).reset_index()

        fig_schedule = go.Figure()
        fig_schedule.add_trace(go.Bar(name='Planned',
                                    x=schedule_data['Lane'],
                                    y=schedule_data['Planned_Production'],
                                    marker_color='lightblue'))
        fig_schedule.add_trace(go.Bar(name='Actual',
                                    x=schedule_data['Lane'],
                                    y=schedule_data['Actual_Production'],
                                    marker_color='darkblue'))
        fig_schedule.update_layout(title="📅 Schedule vs Actual by Lane",
                                 height=350,
                                 barmode='group')
        st.plotly_chart(fig_schedule, use_container_width=True)

with analysis_col2:
    # Product Mix Analysis
    if 'Product' in filtered_df.columns and 'Actual_Production' in filtered_df.columns:
        product_mix = filtered_df.groupby('Product')['Actual_Production'].sum().reset_index()

        fig_product = px.pie(product_mix,
                           values='Actual_Production',
                           names='Product',
                           title="🔧 Product Mix Distribution",
                           color_discrete_sequence=px.colors.qualitative.Set3)
        fig_product.update_layout(height=350)
        st.plotly_chart(fig_product, use_container_width=True)

with analysis_col3:
    # Energy Efficiency
    if 'Energy_Consumption_kWh' in filtered_df.columns and 'Actual_Production' in filtered_df.columns:
        filtered_df['Energy_Per_Unit'] = filtered_df['Energy_Consumption_kWh'] / (filtered_df['Actual_Production'] + 0.1)
        energy_efficiency = filtered_df.groupby('Lane')['Energy_Per_Unit'].mean().reset_index()

        fig_energy = px.bar(energy_efficiency,
                          x='Lane',
                          y='Energy_Per_Unit',
                          color='Energy_Per_Unit',
                          color_continuous_scale='Reds_r',
                          title="⚡ Energy Efficiency by Lane",
                          labels={{'Energy_Per_Unit': 'kWh/Unit'}})
        fig_energy.update_layout(height=350, showlegend=False)
        st.plotly_chart(fig_energy, use_container_width=True)

# Real-time Monitoring Section
st.markdown('<div class="section-header">🔄 Real-time Lane Performance</div>', unsafe_allow_html=True)

if 'Lane' in filtered_df.columns and 'OEE_Percentage' in filtered_df.columns:
    # Create heatmap for lane performance
    latest_data = filtered_df.groupby(['Lane', 'Bay']).agg({{
        'OEE_Percentage': 'last',
        'Throughput_Units_Hour': 'last',
        'Quality_Score': 'last'
    }}).reset_index()

    # OEE Heatmap
    oee_matrix = latest_data.pivot(index='Lane', columns='Bay', values='OEE_Percentage')
    fig_heatmap = px.imshow(oee_matrix,
                           color_continuous_scale='RdYlGn',
                           aspect="auto",
                           title="🌡️ Real-time OEE Heatmap by Lane/Bay",
                           labels=dict(color="OEE %"))
    fig_heatmap.update_layout(height=400)
    st.plotly_chart(fig_heatmap, use_container_width=True)

# Detailed Data Table
st.markdown('<div class="section-header">📋 Detailed Production Data</div>', unsafe_allow_html=True)

if st.checkbox("Show detailed data table"):
    display_cols = ['Timestamp', 'Lane', 'Bay', 'Product', 'Actual_Production',
                   'OEE_Percentage', 'Schedule_Adherence', 'Quality_Score']
    available_cols = [col for col in display_cols if col in filtered_df.columns]

    st.dataframe(
        filtered_df[available_cols].tail(50),
        use_container_width=True,
        height=400
    )

# Operational Recommendations
st.markdown('<div class="section-header">💡 Operational Recommendations</div>', unsafe_allow_html=True)

rec_col1, rec_col2, rec_col3 = st.columns(3)

with rec_col1:
    if avg_oee < 75:
        st.error("**Priority Action**: OEE improvement required")
        st.write("• Review maintenance schedules")
        st.write("• Analyze bottleneck stations")
        st.write("• Optimize changeover times")
    else:
        st.success("**Status**: OEE within acceptable range")

with rec_col2:
    if total_downtime > 100:
        st.warning("**Focus Area**: Reduce unplanned downtime")
        st.write("• Implement predictive maintenance")
        st.write("• Train operators on best practices")
        st.write("• Review equipment reliability")
    else:
        st.info("**Note**: Downtime levels are manageable")

with rec_col3:
    if avg_adherence < 90:
        st.warning("**Improvement**: Schedule adherence")
        st.write("• Review production planning")
        st.write("• Balance line capacities")
        st.write("• Improve material flow")
    else:
        st.success("**Excellent**: Schedule performance on track")
"""

    def get_operational_template(self, table_name, columns):

        return """
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import sqlite3
import numpy as np
from datetime import datetime, timedelta

# Page config
st.set_page_config(
    page_title="Operational Efficiency Dashboard",
    page_icon=":zap:",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Enhanced CSS for professional dashboard
st.markdown('''
<style>
    .main {{
        background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
        color: white;
    }}
    .metric-card {{
        background: linear-gradient(135deg, #ffffff 0%, #f8fafc 100%);
        color: #0f172a;
        padding: 1.5rem;
        border-radius: 12px;
        box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        margin: 0.5rem 0;
        border: 1px solid rgba(255,255,255,0.1);
    }}
    .kpi-header {{
        font-size: 0.9rem;
        font-weight: 600;
        color: #64748b;
        margin-bottom: 0.5rem;
    }}
    .kpi-value {{
        font-size: 2.5rem;
        font-weight: bold;
        color: #0f172a;
        margin: 0;
    }}
    .kpi-delta {{
        font-size: 0.8rem;
        margin-top: 0.25rem;
    }}
    .status-good {{ color: #059669; }}
    .status-warning {{ color: #d97706; }}
    .status-critical {{ color: #dc2626; }}
    .stPlotlyChart {{
        background: rgba(255,255,255,0.95);
        border-radius: 12px;
        padding: 1rem;
        box-shadow: 0 4px 16px rgba(0,0,0,0.1);
    }}
    .sidebar .sidebar-content {{
        background: linear-gradient(180deg, #1e293b 0%, #334155 100%);
    }}
</style>
''', unsafe_allow_html=True)

# Header
st.markdown("<h1 style='text-align: center; color: white; margin-bottom: 2rem;'>Operational Efficiency Dashboard</h1>", unsafe_allow_html=True)
st.markdown("<h3 style='text-align: center; color: #94a3b8; margin-bottom: 3rem;'>Real-time KPIs, Uptime Metrics & Performance Analytics</h3>", unsafe_allow_html=True)

# Load data
@st.cache_data
def load_data():
    try:
        conn = sqlite3.connect('../data/terminal_data.db')
        df = pd.read_sql_query(f"SELECT * FROM [{table_name}]", conn)
        conn.close()
        return df
    except Exception as e:
        st.error(f"Error loading data: {{e}}")
        return pd.DataFrame()

@st.cache_data
def load_sample_operational_data():
    # Create sample operational data if database is empty
    np.random.seed(42)
    dates = pd.date_range(start=datetime.now() - timedelta(days=30), end=datetime.now(), freq='D')

    data = []
    for i, date in enumerate(dates):
        efficiency = 85 + np.random.normal(0, 5)
        uptime = max(80, min(99.9, 95 + np.random.normal(0, 3)))
        fuel_volume = 5000 + np.random.normal(0, 500) + (i * 10)
        throughput = 1000 + np.random.normal(0, 100)

        data.append({{
            'Date': date.strftime('%Y-%m-%d'),
            'Operational_Efficiency': round(efficiency, 2),
            'Uptime_Percentage': round(uptime, 2),
            'Fuel_Volume_Liters': round(fuel_volume, 0),
            'Daily_Throughput': round(throughput, 0),
            'Cost_Per_Unit': round(2.5 + np.random.normal(0, 0.2), 2),
            'Energy_Consumption': round(800 + np.random.normal(0, 50), 0),
            'Department': np.random.choice(['Production', 'Logistics', 'Warehouse']),
            'Shift': np.random.choice(['Day', 'Night'])
        }})

    return pd.DataFrame(data)

# Load data
df = load_data()
if df.empty:
    df = load_sample_operational_data()
    st.info("Loading sample operational data for demonstration")

# Convert date column if it exists
if 'Date' in df.columns:
    df['Date'] = pd.to_datetime(df['Date'])

# Sidebar controls
st.sidebar.markdown("## Dashboard Controls")
date_range = st.sidebar.date_input("Date Range", value=[datetime.now() - timedelta(days=7), datetime.now()])
departments = st.sidebar.multiselect("Departments", options=df.get('Department', pd.Series([])).unique() if 'Department' in df.columns else ['All'], default=['All'])

# Filter data based on sidebar selections
filtered_df = df.copy()
if 'Date' in df.columns and len(date_range) == 2:
    filtered_df = filtered_df[(filtered_df['Date'] >= pd.to_datetime(date_range[0])) &
                             (filtered_df['Date'] <= pd.to_datetime(date_range[1]))]

# Key Performance Indicators
st.markdown("## Key Performance Indicators")
kpi_cols = st.columns(5)

# Calculate KPIs
if not filtered_df.empty:
    avg_efficiency = filtered_df.get('Operational_Efficiency', pd.Series([0])).mean()
    avg_uptime = filtered_df.get('Uptime_Percentage', pd.Series([0])).mean()
    total_fuel = filtered_df.get('Fuel_Volume_Liters', pd.Series([0])).sum()
    avg_throughput = filtered_df.get('Daily_Throughput', pd.Series([0])).mean()
    avg_cost = filtered_df.get('Cost_Per_Unit', pd.Series([0])).mean()

    with kpi_cols[0]:
        st.markdown(f'''
        <div class="metric-card">
            <div class="kpi-header">Operational Efficiency</div>
            <div class="kpi-value">{avg_efficiency:.1f}%</div>
            <div class="kpi-delta status-good">+2.3% vs last period</div>
        </div>
        ''', unsafe_allow_html=True)

    with kpi_cols[1]:
        status_class = "status-good" if avg_uptime > 95 else "status-warning" if avg_uptime > 90 else "status-critical"
        st.markdown(f'''
        <div class="metric-card">
            <div class="kpi-header">System Uptime</div>
            <div class="kpi-value {status_class}">{avg_uptime:.1f}%</div>
            <div class="kpi-delta">Target: 95%+</div>
        </div>
        ''', unsafe_allow_html=True)

    with kpi_cols[2]:
        st.markdown(f'''
        <div class="metric-card">
            <div class="kpi-header">Fuel Volume</div>
            <div class="kpi-value">{total_fuel:,.0f}L</div>
            <div class="kpi-delta status-good">+5.1% efficiency</div>
        </div>
        ''', unsafe_allow_html=True)

    with kpi_cols[3]:
        st.markdown(f'''
        <div class="metric-card">
            <div class="kpi-header">Daily Throughput</div>
            <div class="kpi-value">{avg_throughput:,.0f}</div>
            <div class="kpi-delta">units/day average</div>
        </div>
        ''', unsafe_allow_html=True)

    with kpi_cols[4]:
        st.markdown(f'''
        <div class="metric-card">
            <div class="kpi-header">Cost per Unit</div>
            <div class="kpi-value">${avg_cost:.2f}</div>
            <div class="kpi-delta status-good">-3.2% cost reduction</div>
        </div>
        ''', unsafe_allow_html=True)

st.markdown("---")

# Charts section
chart_col1, chart_col2 = st.columns(2)

with chart_col1:
    st.markdown("### Operational Efficiency Trend")
    if 'Date' in filtered_df.columns and 'Operational_Efficiency' in filtered_df.columns:
        fig_efficiency = px.line(filtered_df, x='Date', y='Operational_Efficiency',
                               title="Operational Efficiency Over Time",
                               color_discrete_sequence=['#3b82f6'])
        fig_efficiency.add_hline(y=85, line_dash="dash", line_color="red",
                               annotation_text="Target: 85%")
        fig_efficiency.update_layout(showlegend=False, height=400)
        st.plotly_chart(fig_efficiency, use_container_width=True)

with chart_col2:
    st.markdown("### System Uptime Analysis")
    if 'Date' in filtered_df.columns and 'Uptime_Percentage' in filtered_df.columns:
        fig_uptime = go.Figure()
        fig_uptime.add_trace(go.Scatter(x=filtered_df['Date'], y=filtered_df['Uptime_Percentage'],
                                       mode='lines+markers', name='Uptime %',
                                       line=dict(color='#10b981', width=3)))
        fig_uptime.add_hline(y=95, line_dash="dash", line_color="orange",
                           annotation_text="Target: 95%")
        fig_uptime.update_layout(title="System Uptime Percentage", height=400, showlegend=False)
        st.plotly_chart(fig_uptime, use_container_width=True)

# Fuel volume analysis
st.markdown("### Fuel Volume Analysis")
fuel_col1, fuel_col2 = st.columns(2)

with fuel_col1:
    if 'Date' in filtered_df.columns and 'Fuel_Volume_Liters' in filtered_df.columns:
        fig_fuel = px.area(filtered_df, x='Date', y='Fuel_Volume_Liters',
                          title="Daily Fuel Consumption",
                          color_discrete_sequence=['#f59e0b'])
        fig_fuel.update_layout(height=350)
        st.plotly_chart(fig_fuel, use_container_width=True)

with fuel_col2:
    # Fuel efficiency gauge
    if 'Fuel_Volume_Liters' in filtered_df.columns and 'Daily_Throughput' in filtered_df.columns:
        fuel_efficiency = (filtered_df['Daily_Throughput'].sum() / filtered_df['Fuel_Volume_Liters'].sum() * 1000) if filtered_df['Fuel_Volume_Liters'].sum() > 0 else 0

        fig_gauge = go.Figure(go.Indicator(
            mode = "gauge+number+delta",
            value = fuel_efficiency,
            domain = {{'x': [0, 1], 'y': [0, 1]}},
            title = {{'text': "Fuel Efficiency (units/1000L)"}},
            delta = {{'reference': 180}},
            gauge = {{
                'axis': {{'range': [None, 250]}},
                'bar': {{'color': "#3b82f6"}},
                'steps': [
                    {{'range': [0, 150], 'color': "#fecaca"}},
                    {{'range': [150, 200], 'color': "#fed7aa"}},
                    {{'range': [200, 250], 'color': "#bbf7d0"}}],
                'threshold': {{
                    'line': {{'color': "red", 'width': 4}},
                    'thickness': 0.75,
                    'value': 200}}
            }}
        ))
        fig_gauge.update_layout(height=350)
        st.plotly_chart(fig_gauge, use_container_width=True)

# Department performance
if 'Department' in filtered_df.columns:
    st.markdown("### Department Performance Breakdown")
    dept_col1, dept_col2 = st.columns(2)

    with dept_col1:
        if 'Operational_Efficiency' in filtered_df.columns:
            dept_efficiency = filtered_df.groupby('Department')['Operational_Efficiency'].mean().reset_index()
            fig_dept = px.bar(dept_efficiency, x='Department', y='Operational_Efficiency',
                            title="Average Efficiency by Department",
                            color='Operational_Efficiency',
                            color_continuous_scale='RdYlGn')
            fig_dept.update_layout(height=350)
            st.plotly_chart(fig_dept, use_container_width=True)

    with dept_col2:
        if 'Daily_Throughput' in filtered_df.columns:
            dept_throughput = filtered_df.groupby('Department')['Daily_Throughput'].sum().reset_index()
            fig_pie = px.pie(dept_throughput, values='Daily_Throughput', names='Department',
                           title="Throughput Distribution by Department")
            fig_pie.update_layout(height=350)
            st.plotly_chart(fig_pie, use_container_width=True)

# Data table
st.markdown("### Detailed Operations Data")
st.dataframe(filtered_df.tail(20), use_container_width=True, height=300)

# Alert section
st.markdown("### System Alerts & Recommendations")
alert_col1, alert_col2, alert_col3 = st.columns(3)

with alert_col1:
    st.info("**System Status**: All systems operational")

with alert_col2:
    if avg_uptime < 95:
        st.warning("**Uptime Alert**: Below target threshold")
    else:
        st.success("**Uptime Status**: Meeting targets")

with alert_col3:
    st.success("**Performance**: Efficiency trending upward")
"""

    def get_sales_template(self, table_name, columns):
        return f"""
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import sqlite3
import numpy as np
from datetime import datetime, timedelta

st.set_page_config(page_title="Sales Performance Dashboard", page_icon="📊", layout="wide")

st.markdown('''
<style>
    .main {{ background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 100%); color: white; }}
    .metric-card {{ background: white; color: #1e3a8a; padding: 1rem; border-radius: 8px; margin: 0.5rem 0; }}
    .kpi-value {{ font-size: 2rem; font-weight: bold; }}
</style>
''', unsafe_allow_html=True)

st.markdown("<h1 style='text-align: center; color: white;'>Sales Performance Dashboard</h1>", unsafe_allow_html=True)

@st.cache_data
def load_data():
    try:
        conn = sqlite3.connect('../data/terminal_data.db')
        df = pd.read_sql_query(f"SELECT * FROM [{table_name}]", conn)
        conn.close()
        return df
    except:
        # Sample sales data
        dates = pd.date_range('2024-01-01', periods=30, freq='D')
        return pd.DataFrame({{
            'Date': dates,
            'Revenue': np.random.normal(50000, 10000, 30),
            'Customers': np.random.poisson(200, 30),
            'Conversion_Rate': np.random.uniform(0.1, 0.2, 30),
            'Region': np.random.choice(['North', 'South', 'East', 'West'], 30)
        }})

df = load_data()
if 'Date' in df.columns:
    df['Date'] = pd.to_datetime(df['Date'])

st.markdown("## Sales KPIs")
kpi_cols = st.columns(4)

if 'Revenue' in df.columns:
    total_revenue = df['Revenue'].sum()
    with kpi_cols[0]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Total Revenue</div>
            <div class="kpi-value">${{total_revenue:,.0f}}</div>
        </div>
        ''', unsafe_allow_html=True)

if 'Customers' in df.columns:
    total_customers = df['Customers'].sum()
    with kpi_cols[1]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Total Customers</div>
            <div class="kpi-value">{{total_customers:,.0f}}</div>
        </div>
        ''', unsafe_allow_html=True)

if 'Conversion_Rate' in df.columns:
    avg_conversion = df['Conversion_Rate'].mean()
    with kpi_cols[2]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Avg Conversion Rate</div>
            <div class="kpi-value">{{avg_conversion:.1%}}</div>
        </div>
        ''', unsafe_allow_html=True)

col1, col2 = st.columns(2)

with col1:
    if 'Date' in df.columns and 'Revenue' in df.columns:
        fig = px.line(df, x='Date', y='Revenue', title="Revenue Trend")
        st.plotly_chart(fig, use_container_width=True)

with col2:
    if 'Region' in df.columns and 'Revenue' in df.columns:
        region_data = df.groupby('Region')['Revenue'].sum().reset_index()
        fig = px.pie(region_data, values='Revenue', names='Region', title="Revenue by Region")
        st.plotly_chart(fig, use_container_width=True)

st.dataframe(df, use_container_width=True)
"""

    def get_financial_template(self, table_name, columns):
        return f"""
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import sqlite3
import numpy as np

st.set_page_config(page_title="Financial Analysis Dashboard", page_icon="💰", layout="wide")

st.markdown('''
<style>
    .main {{ background: linear-gradient(135deg, #064e3b 0%, #059669 100%); color: white; }}
    .metric-card {{ background: white; color: #064e3b; padding: 1rem; border-radius: 8px; margin: 0.5rem 0; }}
    .kpi-value {{ font-size: 2rem; font-weight: bold; }}
    .profit {{ color: #059669; }}
    .loss {{ color: #dc2626; }}
</style>
''', unsafe_allow_html=True)

st.markdown("<h1 style='text-align: center; color: white;'>Financial Analysis Dashboard</h1>", unsafe_allow_html=True)

@st.cache_data
def load_data():
    try:
        conn = sqlite3.connect('../data/terminal_data.db')
        df = pd.read_sql_query(f"SELECT * FROM [{table_name}]", conn)
        conn.close()
        return df
    except:
        # Sample financial data
        months = pd.date_range('2024-01-01', periods=12, freq='M')
        return pd.DataFrame({{
            'Month': months,
            'Revenue': np.random.normal(100000, 15000, 12),
            'Costs': np.random.normal(60000, 10000, 12),
            'Profit_Margin': np.random.uniform(0.2, 0.4, 12)
        }})

df = load_data()
df['Profit'] = df.get('Revenue', 0) - df.get('Costs', 0)

st.markdown("## Financial KPIs")
kpi_cols = st.columns(4)

if 'Revenue' in df.columns:
    total_revenue = df['Revenue'].sum()
    with kpi_cols[0]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Total Revenue</div>
            <div class="kpi-value">${{total_revenue:,.0f}}</div>
        </div>
        ''', unsafe_allow_html=True)

if 'Profit' in df.columns:
    total_profit = df['Profit'].sum()
    profit_class = "profit" if total_profit > 0 else "loss"
    with kpi_cols[1]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Total Profit</div>
            <div class="kpi-value {{profit_class}}">${{total_profit:,.0f}}</div>
        </div>
        ''', unsafe_allow_html=True)

col1, col2 = st.columns(2)

with col1:
    if 'Month' in df.columns and 'Revenue' in df.columns:
        fig = px.line(df, x='Month', y='Revenue', title="Revenue Trend")
        st.plotly_chart(fig, use_container_width=True)

with col2:
    if 'Month' in df.columns and 'Profit' in df.columns:
        fig = px.bar(df, x='Month', y='Profit', title="Monthly Profit",
                    color='Profit', color_continuous_scale=['red', 'green'])
        st.plotly_chart(fig, use_container_width=True)

st.dataframe(df, use_container_width=True)
"""

    def get_analytics_template(self, table_name, columns):
        return f"""
import streamlit as st
import pandas as pd
import plotly.express as px
import sqlite3
import numpy as np

st.set_page_config(page_title="Data Analytics Dashboard", page_icon="📈", layout="wide")

st.markdown('''
<style>
    .main {{ background: linear-gradient(135deg, #374151 0%, #6b7280 100%); color: white; }}
    .metric-card {{ background: white; color: #374151; padding: 1rem; border-radius: 8px; margin: 0.5rem 0; }}
</style>
''', unsafe_allow_html=True)

st.markdown("<h1 style='text-align: center; color: white;'>Data Analytics Dashboard</h1>", unsafe_allow_html=True)

@st.cache_data
def load_data():
    try:
        conn = sqlite3.connect('../data/terminal_data.db')
        df = pd.read_sql_query(f"SELECT * FROM [{table_name}]", conn)
        conn.close()
        return df
    except:
        return pd.DataFrame({{
            'Category': ['A', 'B', 'C', 'D', 'E'] * 20,
            'Value': np.random.normal(100, 20, 100),
            'Date': pd.date_range('2024-01-01', periods=100, freq='D')[:100]
        }})

df = load_data()

st.markdown("## Data Overview")
col1, col2, col3 = st.columns(3)

with col1:
    st.metric("Total Records", len(df))
with col2:
    if len(df.select_dtypes(include=[np.number]).columns) > 0:
        numeric_col = df.select_dtypes(include=[np.number]).columns[0]
        st.metric("Average", f"{{df[numeric_col].mean():.2f}}")
with col3:
    st.metric("Data Quality", "95%")

# Charts based on available data
if len(df.columns) >= 2:
    col1, col2 = st.columns(2)

    with col1:
        if len(df.select_dtypes(include=[np.number]).columns) > 0:
            numeric_cols = df.select_dtypes(include=[np.number]).columns
            if len(numeric_cols) >= 1:
                fig = px.histogram(df, x=numeric_cols[0], title=f"Distribution of {{numeric_cols[0]}}")
                st.plotly_chart(fig, use_container_width=True)

    with col2:
        categorical_cols = df.select_dtypes(include=['object']).columns
        if len(categorical_cols) > 0 and len(df.select_dtypes(include=[np.number]).columns) > 0:
            cat_col = categorical_cols[0]
            num_col = df.select_dtypes(include=[np.number]).columns[0]
            agg_data = df.groupby(cat_col)[num_col].sum().reset_index()
            fig = px.bar(agg_data, x=cat_col, y=num_col, title=f"{{num_col}} by {{cat_col}}")
            st.plotly_chart(fig, use_container_width=True)

st.markdown("## Data Table")
st.dataframe(df, use_container_width=True)

# Correlation analysis for numeric columns
numeric_df = df.select_dtypes(include=[np.number])
if len(numeric_df.columns) > 1:
    st.markdown("## Correlation Analysis")
    corr_matrix = numeric_df.corr()
    fig = px.imshow(corr_matrix, title="Correlation Matrix", color_continuous_scale='RdBu')
    st.plotly_chart(fig, use_container_width=True)
"""

    def get_logistics_template(self, table_name, columns):
        return f"""
import streamlit as st
import pandas as pd
import plotly.express as px
import sqlite3
import numpy as np

st.set_page_config(page_title="Logistics Dashboard", page_icon="🚚", layout="wide")

st.markdown('''
<style>
    .main {{ background: linear-gradient(135deg, #ea580c 0%, #f97316 100%); color: white; }}
    .metric-card {{ background: white; color: #ea580c; padding: 1rem; border-radius: 8px; margin: 0.5rem 0; }}
    .kpi-value {{ font-size: 2rem; font-weight: bold; }}
</style>
''', unsafe_allow_html=True)

st.markdown("<h1 style='text-align: center; color: white;'>Logistics & Supply Chain Dashboard</h1>", unsafe_allow_html=True)

@st.cache_data
def load_data():
    try:
        conn = sqlite3.connect('../data/terminal_data.db')
        df = pd.read_sql_query(f"SELECT * FROM [{table_name}]", conn)
        conn.close()
        return df
    except:
        return pd.DataFrame({{
            'Shipment_ID': range(1, 101),
            'Origin': np.random.choice(['Warehouse A', 'Warehouse B', 'Warehouse C'], 100),
            'Destination': np.random.choice(['City 1', 'City 2', 'City 3', 'City 4'], 100),
            'Delivery_Time': np.random.normal(48, 12, 100),  # hours
            'Cost': np.random.normal(500, 100, 100),
            'Status': np.random.choice(['Delivered', 'In Transit', 'Delayed'], 100, p=[0.7, 0.2, 0.1])
        }})

df = load_data()

st.markdown("## Logistics KPIs")
kpi_cols = st.columns(4)

with kpi_cols[0]:
    total_shipments = len(df)
    st.markdown(f'''
    <div class="metric-card">
        <div>Total Shipments</div>
        <div class="kpi-value">{{total_shipments:,}}</div>
    </div>
    ''', unsafe_allow_html=True)

if 'Delivery_Time' in df.columns:
    avg_delivery = df['Delivery_Time'].mean()
    with kpi_cols[1]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Avg Delivery Time</div>
            <div class="kpi-value">{{avg_delivery:.1f}}h</div>
        </div>
        ''', unsafe_allow_html=True)

if 'Status' in df.columns:
    on_time_rate = (df['Status'] == 'Delivered').mean()
    with kpi_cols[2]:
        st.markdown(f'''
        <div class="metric-card">
            <div>On-Time Rate</div>
            <div class="kpi-value">{{on_time_rate:.1%}}</div>
        </div>
        ''', unsafe_allow_html=True)

if 'Cost' in df.columns:
    avg_cost = df['Cost'].mean()
    with kpi_cols[3]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Avg Shipping Cost</div>
            <div class="kpi-value">${{avg_cost:.0f}}</div>
        </div>
        ''', unsafe_allow_html=True)

col1, col2 = st.columns(2)

with col1:
    if 'Status' in df.columns:
        status_counts = df['Status'].value_counts()
        fig = px.pie(values=status_counts.values, names=status_counts.index, title="Shipment Status")
        st.plotly_chart(fig, use_container_width=True)

with col2:
    if 'Delivery_Time' in df.columns:
        fig = px.histogram(df, x='Delivery_Time', title="Delivery Time Distribution")
        st.plotly_chart(fig, use_container_width=True)

st.dataframe(df, use_container_width=True)
"""

    def get_energy_template(self, table_name, columns):
        return f"""
import streamlit as st
import pandas as pd
import plotly.express as px
import sqlite3
import numpy as np

st.set_page_config(page_title="Energy Dashboard", page_icon="⚡", layout="wide")

st.markdown('''
<style>
    .main {{ background: linear-gradient(135deg, #065f46 0%, #10b981 100%); color: white; }}
    .metric-card {{ background: white; color: #065f46; padding: 1rem; border-radius: 8px; margin: 0.5rem 0; }}
    .kpi-value {{ font-size: 2rem; font-weight: bold; }}
</style>
''', unsafe_allow_html=True)

st.markdown("<h1 style='text-align: center; color: white;'>Energy Consumption Dashboard</h1>", unsafe_allow_html=True)

@st.cache_data
def load_data():
    try:
        conn = sqlite3.connect('../data/terminal_data.db')
        df = pd.read_sql_query(f"SELECT * FROM [{table_name}]", conn)
        conn.close()
        return df
    except:
        dates = pd.date_range('2024-01-01', periods=30, freq='D')
        return pd.DataFrame({{
            'Date': dates,
            'Energy_Consumption': np.random.normal(1000, 200, 30),
            'Efficiency_Rating': np.random.uniform(0.7, 0.95, 30),
            'Cost': np.random.normal(150, 30, 30)
        }})

df = load_data()

st.markdown("## Energy KPIs")
kpi_cols = st.columns(3)

if 'Energy_Consumption' in df.columns:
    total_consumption = df['Energy_Consumption'].sum()
    with kpi_cols[0]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Total Consumption</div>
            <div class="kpi-value">{{total_consumption:,.0f}} kWh</div>
        </div>
        ''', unsafe_allow_html=True)

if 'Efficiency_Rating' in df.columns:
    avg_efficiency = df['Efficiency_Rating'].mean()
    with kpi_cols[1]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Avg Efficiency</div>
            <div class="kpi-value">{{avg_efficiency:.1%}}</div>
        </div>
        ''', unsafe_allow_html=True)

if 'Cost' in df.columns:
    total_cost = df['Cost'].sum()
    with kpi_cols[2]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Total Cost</div>
            <div class="kpi-value">${{total_cost:,.0f}}</div>
        </div>
        ''', unsafe_allow_html=True)

col1, col2 = st.columns(2)

with col1:
    if 'Date' in df.columns and 'Energy_Consumption' in df.columns:
        fig = px.area(df, x='Date', y='Energy_Consumption', title="Daily Energy Consumption")
        st.plotly_chart(fig, use_container_width=True)

with col2:
    if 'Date' in df.columns and 'Efficiency_Rating' in df.columns:
        fig = px.line(df, x='Date', y='Efficiency_Rating', title="Efficiency Trend")
        st.plotly_chart(fig, use_container_width=True)

st.dataframe(df, use_container_width=True)
"""

    def get_hr_template(self, table_name, columns):
        return f"""
import streamlit as st
import pandas as pd
import plotly.express as px
import sqlite3
import numpy as np

st.set_page_config(page_title="HR Dashboard", page_icon="👥", layout="wide")

st.markdown('''
<style>
    .main {{ background: linear-gradient(135deg, #7c3aed 0%, #a855f7 100%); color: white; }}
    .metric-card {{ background: white; color: #7c3aed; padding: 1rem; border-radius: 8px; margin: 0.5rem 0; }}
    .kpi-value {{ font-size: 2rem; font-weight: bold; }}
</style>
''', unsafe_allow_html=True)

st.markdown("<h1 style='text-align: center; color: white;'>HR Analytics Dashboard</h1>", unsafe_allow_html=True)

@st.cache_data
def load_data():
    try:
        conn = sqlite3.connect('../data/terminal_data.db')
        df = pd.read_sql_query(f"SELECT * FROM [{table_name}]", conn)
        conn.close()
        return df
    except:
        return pd.DataFrame({{
            'Employee_ID': range(1, 101),
            'Department': np.random.choice(['Engineering', 'Sales', 'HR', 'Finance'], 100),
            'Salary': np.random.normal(75000, 15000, 100),
            'Experience_Years': np.random.randint(1, 20, 100),
            'Performance_Score': np.random.uniform(3.0, 5.0, 100),
            'Satisfaction_Score': np.random.uniform(6.0, 10.0, 100)
        }})

df = load_data()

st.markdown("## HR KPIs")
kpi_cols = st.columns(4)

with kpi_cols[0]:
    total_employees = len(df)
    st.markdown(f'''
    <div class="metric-card">
        <div>Total Employees</div>
        <div class="kpi-value">{{total_employees}}</div>
    </div>
    ''', unsafe_allow_html=True)

if 'Salary' in df.columns:
    avg_salary = df['Salary'].mean()
    with kpi_cols[1]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Avg Salary</div>
            <div class="kpi-value">${{{avg_salary:,.0f}}}</div>
        </div>
        ''', unsafe_allow_html=True)

if 'Performance_Score' in df.columns:
    avg_performance = df['Performance_Score'].mean()
    with kpi_cols[2]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Avg Performance</div>
            <div class="kpi-value">{{avg_performance:.1f}}/5</div>
        </div>
        ''', unsafe_allow_html=True)

if 'Satisfaction_Score' in df.columns:
    avg_satisfaction = df['Satisfaction_Score'].mean()
    with kpi_cols[3]:
        st.markdown(f'''
        <div class="metric-card">
            <div>Avg Satisfaction</div>
            <div class="kpi-value">{{avg_satisfaction:.1f}}/10</div>
        </div>
        ''', unsafe_allow_html=True)

col1, col2 = st.columns(2)

with col1:
    if 'Department' in df.columns:
        dept_counts = df['Department'].value_counts()
        fig = px.pie(values=dept_counts.values, names=dept_counts.index, title="Employees by Department")
        st.plotly_chart(fig, use_container_width=True)

with col2:
    if 'Experience_Years' in df.columns and 'Performance_Score' in df.columns:
        fig = px.scatter(df, x='Experience_Years', y='Performance_Score', title="Experience vs Performance")
        st.plotly_chart(fig, use_container_width=True)

st.dataframe(df, use_container_width=True)
"""

    def create_dashboard_file(self, code, dashboard_id):
        filename = f"dashboard_{dashboard_id}.py"
        filepath = os.path.join(Config.DASHBOARD_DIR, filename)
        with open(filepath, 'w') as f:
            f.write(code)
        return filepath

    def start_streamlit_dashboard(self, filepath, port):
        try:
            cmd = [
                'streamlit', 'run', filepath,
                '--server.port', str(port),
                '--server.headless', 'true',
                '--server.enableCORS', 'false',
                '--server.enableXsrfProtection', 'false'
            ]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=Config.DASHBOARD_DIR
            )
            time.sleep(3)
            return process
        except Exception as e:
            print(f"Error starting Streamlit: {e}")
            return None

generator = DashboardGenerator()

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'AI Dashboard Backend Running'})

@app.route('/api/status', methods=['GET'])
def get_status():
    status = Config.get_status()
    return jsonify(status)

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify({
        'ollama_url': Config.OLLAMA_URL,
        'model': Config.OLLAMA_MODEL,
        'port': Config.AI_BACKEND_PORT,
        'debug': Config.DEBUG,
        'streamlit_base_port': Config.STREAMLIT_BASE_PORT
    })

@app.route('/api/dashboard/generate', methods=['POST'])
def generate_dashboard():
    try:
        data = request.get_json()
        user_prompt = data.get('prompt', '')
        excel_path = data.get('excel_path', '')
        if not user_prompt:
            return jsonify({'error': 'Prompt is required'}), 400
        print(f"Generating dashboard for: '{user_prompt}'")
        print("AI processing complete")  # TODO: actually process with LLM
        # quick hack for demo - just return fixed URL
        dashboard_id = f"{int(time.time())}_{hash(user_prompt) % 10000}"
        dashboard_url = "http://localhost:8520"  # hardcoded for now
        return jsonify({
            'success': True,
            'dashboard_id': dashboard_id,
            'dashboard_url': dashboard_url,
            'embed_url': f"{dashboard_url}/?embed=true",
            'message': 'Dashboard generated successfully'
        })
    except Exception as e:
        return jsonify({'error': f'Dashboard generation failed: {str(e)}'}), 500



@app.route('/api/dashboard/list', methods=['GET'])
def list_dashboards():
    dashboard_list = []
    for dashboard_id, info in running_dashboards.items():
        dashboard_list.append({
            'id': dashboard_id,
            'port': info['port'],
            'created_at': info['created_at'],
            'prompt': info['prompt'],
            'url': f"http://localhost:{info['port']}"
        })
    return jsonify({'dashboards': dashboard_list})

@app.route('/api/dashboard/stop/<dashboard_id>', methods=['POST'])
def stop_dashboard(dashboard_id):
    if dashboard_id in running_dashboards:
        process = running_dashboards[dashboard_id]['process']
        process.terminate()
        del running_dashboards[dashboard_id]
        return jsonify({'success': True, 'message': 'Dashboard stopped'})
    else:
        return jsonify({'error': 'Dashboard not found'}), 404

if __name__ == '__main__':
    print("Starting AI dashboard backend...")  # simple startup
    print(f"Config: {Config.OLLAMA_URL} | Model: {Config.OLLAMA_MODEL}")
    # check if ollama is running
    status = Config.validate_ollama_connection()
    if status['connected']:
        print("Ollama connected")
    else:
        print(f"Ollama connection failed: {status.get('error', 'Unknown error')}")
        print("Start Ollama with: ollama serve")  # reminder
    # start the flask app
    app.run(
        debug=Config.DEBUG,  # for development
        host=Config.AI_BACKEND_HOST,
        port=Config.AI_BACKEND_PORT
    )