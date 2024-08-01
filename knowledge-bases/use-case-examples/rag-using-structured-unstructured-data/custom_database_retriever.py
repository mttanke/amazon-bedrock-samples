"""
The provided code defines a retriever class called `AmazonAthenaRetriever` that retrieves relevant data from an Amazon Athena database using SQL queries generated by Amazon Bedrock. The retriever interacts with the Athena database through the AWS boto3 SDK, which allows running SQL queries on data stored in Amazon S3. It generates SQL queries based on natural language input, executes the queries on Athena, and returns the results as a list of documents formatted for a LangChain RetrievalQA chain.

Important Note: While the code demonstrates the use of Athena, you can adapt it to work with other databases like Redshift or RDS by using the appropriate Data APIs instead of Athena.
"""

from typing import Any, Dict, List, Optional

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.pydantic_v1 import BaseModel, root_validator
from langchain_core.retrievers import BaseRetriever
import time
import json


class VectorSearchConfig(BaseModel, extra="allow"):  # type: ignore[call-arg]
    """Configuration for vector search."""

    numberOfResults: int = 4


class RetrievalConfig(BaseModel, extra="allow"):  # type: ignore[call-arg]
    """Configuration for retrieval."""

    vectorSearchConfiguration: VectorSearchConfig


class AmazonAthenaRetriever(BaseRetriever):
    """`Amazon Bedrock Knowledge Bases` retrieval.

    See https://aws.amazon.com/bedrock/knowledge-bases for more info.

    Args:
        knowledge_base_id: Knowledge Base ID.
        region_name: The aws region e.g., `us-west-2`.
            Fallback to AWS_DEFAULT_REGION env variable or region specified in
            ~/.aws/config.
        credentials_profile_name: The name of the profile in the ~/.aws/credentials
            or ~/.aws/config files, which has either access keys or role information
            specified. If not specified, the default credential profile or, if on an
            EC2 instance, credentials from IMDS will be used.
        client: boto3 client for bedrock agent runtime.
        retrieval_config: Configuration for retrieval.

    Example:
        .. code-block:: python

            from langchain_community.retrievers import AmazonKnowledgeBasesRetriever

            retriever = AmazonKnowledgeBasesRetriever(
                knowledge_base_id="<knowledge-base-id>",
                retrieval_config={
                    "vectorSearchConfiguration": {
                        "numberOfResults": 4
                    }
                },
            )
    """

    database: str
    RESULT_OUTPUT_LOCATION: str
    region_name: Optional[str] = None
    credentials_profile_name: Optional[str] = None
    endpoint_url: Optional[str] = None
    athena_client: Any
    bedrock_client: Any
    model_id: str

    @root_validator(pre=True)
    def create_client(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if values.get("athena_client") is not None:
            return values
        else:
            raise ValueError(
                "Could not load credentials to authenticate with AWS client. "
                "Please check that credentials in the specified "
                "profile name are valid."
            )
            
        if values.get("bedrock_client") is not None:
            return values
        else:
            raise ValueError(
                "Could not load credentials to authenticate with AWS client. "
                "Please check that credentials in the specified "
                "profile name are valid."
            )
            
    def sql_generator(self, bedrock_client, model_id, messages, system_prompt):
        """Prompt-based model to extract entities from the document."""
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "messages": messages,
                "max_tokens": 4000,
                "temperature": 0,
                "top_p": 0.00,
                "system": system_prompt,
            }
        )
        accept = "application/json"
        content_type = "application/json"
        try:
            response = bedrock_client.invoke_model(body=body, modelId=model_id)
            response_body = json.loads(response.get("body").read())
        except Exception as e:
            raise e
        return response_body


    def generate_sql_query(self, question: str) -> str:
        """
        Generate SQL query based on the given question using Bedrock.

        Args:
            question (str): The question to generate the SQL query for.

        Returns:
            str: The generated SQL query.
        """
        if self.bedrock_client is None:
            raise ValueError("Bedrock client is not initialized.")

        dialect = "prestodb"
        list_of_tables = "orders, order_items, reviews, payments"
        table_info = """
        orders:
            - order_id STRING, -- Unique identifier for the order
            - customer_id STRING, -- Identifier of the customer who placed the order
            - order_total FLOAT, -- Total amount of the order
            - order_status STRING, -- Current status of the order, e.g., pending, shipped, delivered
            - payment_method STRING, -- Payment method used for the order
            - shipping_address STRING, -- Shipping address for the order
            - created_at BIGINT, -- Timestamp when the order was placed
            - updated_at BIGINT -- Timestamp when the order status was last updated

        order_items:
            - order_item_id STRING, -- Unique identifier for the order item
            - order_id STRING, -- Identifier of the order this item belongs to
            - product_id STRING, -- Identifier of the product in this order item
            - quantity INT, -- Quantity of the product in this order item
            - price FLOAT -- Price of the product at the time of order

        reviews:
            - review_id STRING, -- Unique identifier for the review
            - product_id STRING, -- Identifier of the product being reviewed
            - customer_id STRING, -- Identifier of the customer who wrote the review
            - rating INT, -- Rating given by the customer, e.g., 1-5 stars
            - created_at BIGINT -- Timestamp when the review was written

        payments:
            - payment_id STRING, -- Unique identifier for the payment
            - order_id STRING, -- Identifier of the order this payment is for, must also be in order_items and orders table
            - customer_id STRING, -- Identifier of the customer who made the payment
            - amount FLOAT, -- Amount paid, always positive
            - payment_method STRING, -- Payment method used, e.g., credit card, PayPal
            - payment_status STRING, -- Status of the payment, e.g., success, failed, refunds
            - created_at BIGINT -- Timestamp when the payment was made
        """
        
        few_shot_examples = """
        -- Top 5 customers that placed most amount of orders
        SELECT customer_id, count(distinct order_id) as total_orders_placed
        FROM orders
        GROUP BY 1
        ORDER BY total_orders_placed DESC
        LIMIT 5;
        
        -- Find how many orders were refunded and whats the total number of orders placed by payment_method
        SELECT payment_method, count(case when payment_status = 'refunds' then 1 else 0 end) as total_refund_orders,
        count(distinct order_id) as total_orders_placed,
        FROM payments
        GROUP BY payment_method;
        """

        system_prompt_for_sql_generation = """
        Transform the following natural language requests into valid {} SQL queries on AWS Athena. You are only ALLOWED to use the following four tables {} and their schema: {}
        
        Here are some examples for you to look at, see how a column name has table alias. You must define meaningful column names as well: {}

        Provide the SQL query that would retrieve the data based on the natural language request, try to provide raw data as possible and not aggregate. Do not add preamble or additional information.
        """
        
        user_prompt = "{}"

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": user_prompt.format(question),
                    }
                ],
            }
        ]

        response = self.sql_generator(self.bedrock_client, 
                                      self.model_id, 
                                      messages,
                                     system_prompt_for_sql_generation.format(dialect, 
                                                                             list_of_tables, 
                                                                             table_info,
                                                                             few_shot_examples)
                                     )
        sql_query = response["content"][0]["text"]

        return sql_query


    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> List[Document]:
        
        # Invoke Bedock to write the query:
        SQLQuery = self.generate_sql_query(query)
        
        # Execute The query:
        response = self.athena_client.start_query_execution(
            QueryString=SQLQuery,
            QueryExecutionContext={'Database': self.database},
            ResultConfiguration={"OutputLocation": self.RESULT_OUTPUT_LOCATION}
        )
        executionID = response['QueryExecutionId']      
        # check if results can be fetched:
        state = "RUNNING"
        max_execution = 20
        fetch_results = False
        
        while max_execution > 0 and fetch_results == False:
            max_execution -= 1
            queryExecutionStatus = self.athena_client.get_query_execution(QueryExecutionId=executionID)
            if (
                "QueryExecution" in queryExecutionStatus
                and "Status" in queryExecutionStatus["QueryExecution"]
                and "State" in queryExecutionStatus["QueryExecution"]["Status"]
            ):
                state = queryExecutionStatus["QueryExecution"]["Status"]["State"]
                if state == "SUCCEEDED":
                    fetch_results = True
                   
            # optimize per query runtin distribution from DB
            if max_execution % 5 == 0:
                time.sleep(3)
            else:
                time.sleep(0.5)
        
        print(max_execution)

        # Fetch the results:
        athenaResults = None # setting None such that LLM can say it doesn't have context to answer in case Query failure
        
        if fetch_results == True:
            athenaResults = self.athena_client.get_query_results(
                QueryExecutionId=executionID
            )

        # Process documents for RAG:
        documents = []
        if athenaResults is not None:
            list_of_rows = athenaResults['ResultSet']['Rows']
            value_str = ''
            for ind in range(len(list_of_rows)):
                values = [d['VarCharValue'] for d in list_of_rows[ind]['Data']]
                value_str += f"[{', '.join(values)}]"
            # to add each row as a document retrieved, move below line in the FOR loop:
            documents.append(
                Document(
                    page_content=value_str
                )
            )
        else:
            documents.append(
                    Document(
                        page_content='NA'
                    )
                )
            
        # add Metadata as a document for context
        documents.append(
            Document(
                    page_content='',
                    metadata={
                        "executionID": executionID,
                        "sqlQuery": SQLQuery,
                        "queryState":state
                    },
                )
        )
        return documents