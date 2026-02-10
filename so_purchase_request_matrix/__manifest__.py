{
    "name": "SO Purchase Request Matrix",
    "summary": "Create Purchase Requests from Sales Orders with vendor comparison matrix and allocations",
    'version': '18.0.1.0',
    'category': 'Purchases',
    'author': 'Seivina, LLC',
    'license': '',
    "depends": [
        "sale_management",
        "purchase",
        "mail",
        "web",
    ],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/sequence.xml",
        "views/purchase_request_views.xml",
        "views/sale_order_views.xml",
        "views/purchase_order_views.xml",
        "wizard/so_create_request_views.xml",
        "views/assets.xml",
        "views/menu.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "so_purchase_request_matrix/static/src/js/prq_matrix_widget.js",
            "so_purchase_request_matrix/static/src/xml/prq_matrix_templates.xml",
            "so_purchase_request_matrix/static/src/scss/prq_matrix.scss",
        ],
    },
    "application": True,
    "installable": True,
}
