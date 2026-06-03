import re
from collections import Counter

with open('ispo_debug.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Find all capitalized phrases
words = re.findall(r'\b([A-Z][A-Za-z0-9\s\-\&\.]{2,40})\b', html)
c = Counter(words)

# Common stop words to filter out
stop = {'The', 'And', 'For', 'With', 'Your', 'More', 'Home', 'About', 'Contact', 'Search', 'Menu',
        'Skip', 'Link', 'Main', 'Content', 'Footer', 'Header', 'Navigation', 'Page', 'Loading',
        'Close', 'Open', 'Back', 'Next', 'Previous', 'Submit', 'Cancel', 'English', 'Deutsch',
        'Cookie', 'Privacy', 'Terms', 'Conditions', 'Imprint', 'All', 'Rights', 'Reserved',
        'Copyright', 'Follow', 'Share', 'Like', 'Tweet', 'Pin', 'Email', 'Print', 'Download',
        'View', 'Read', 'Learn', 'Discover', 'Explore', 'Find', 'Get', 'Start', 'Join', 'Sign',
        'Log', 'Register', 'Account', 'Profile', 'Settings', 'Help', 'Support', 'FAQ', 'Blog',
        'News', 'Events', 'Press', 'Media', 'Careers', 'Jobs', 'Partners', 'Suppliers',
        'Exhibitors', 'Visitors', 'Tickets', 'My', 'Cart', 'Checkout', 'Wishlist', 'Compare',
        'Save', 'Add', 'Remove', 'Delete', 'Edit', 'Update', 'Create', 'New', 'Old', 'First',
        'Last', 'Latest', 'Popular', 'Trending', 'Featured', 'Recommended', 'Related', 'Similar',
        'Other', 'Another', 'Same', 'Different', 'Best', 'Top', 'High', 'Low', 'Big', 'Small',
        'Large', 'Medium', 'Long', 'Short', 'Wide', 'Narrow', 'Deep', 'Shallow', 'Full', 'Empty',
        'Free', 'Paid', 'Public', 'Private', 'Internal', 'External', 'Local', 'Global',
        'International', 'National', 'Regional', 'Online', 'Offline', 'Digital', 'Physical',
        'Virtual', 'Real', 'Live', 'Recorded', 'Scheduled', 'On', 'Demand', 'Instant', 'Fast',
        'Slow', 'Quick', 'Easy', 'Simple', 'Complex', 'Advanced', 'Basic', 'Standard', 'Premium',
        'Pro', 'Lite', 'Max', 'Mini', 'Ultra', 'Super', 'Mega', 'Hyper', 'Auto', 'Manual', 'Smart',
        'Intelligent', 'Artificial', 'Natural', 'Organic', 'Eco', 'Green', 'Clean', 'Safe',
        'Secure', 'Protected', 'Verified', 'Certified', 'Approved', 'Licensed', 'Registered',
        'Trademark', 'Patent', 'Brand', 'Product', 'Service', 'Solution', 'Platform', 'System',
        'Application', 'Tool', 'Software', 'Hardware', 'Device', 'Equipment', 'Machine',
        'Instrument', 'Appliance', 'Gadget', 'Widget', 'Accessory', 'Part', 'Component', 'Module',
        'Unit', 'Item', 'Piece', 'Set', 'Kit', 'Pack', 'Box', 'Case', 'Bag', 'Container',
        'Package', 'Bundle', 'Collection', 'Series', 'Line', 'Range', 'Category', 'Type', 'Kind',
        'Sort', 'Class', 'Group', 'Family', 'Edition', 'Version', 'Model', 'Style', 'Design',
        'Pattern', 'Color', 'Size', 'Shape', 'Form', 'Format', 'Layout', 'Structure', 'Framework',
        'Architecture', 'Schema', 'Blueprint', 'Plan', 'Map', 'Guide', 'Directory', 'Index',
        'Catalog', 'Listing', 'Database', 'Repository', 'Archive', 'Library', 'Gallery',
        'Portfolio', 'Showcase', 'Display', 'Exhibition', 'Fair', 'Show', 'Event', 'Conference',
        'Convention', 'Summit', 'Forum', 'Symposium', 'Workshop', 'Seminar', 'Webinar', 'Meeting',
        'Session', 'Talk', 'Presentation', 'Lecture', 'Speech', 'Debate', 'Panel', 'Roundtable',
        'Interview', 'Question', 'Response', 'Reply', 'Comment', 'Review', 'Feedback', 'Rating',
        'Score', 'Rank', 'Rate', 'Vote', 'Poll', 'Survey', 'Quiz', 'Test', 'Assessment',
        'Evaluation', 'Analysis', 'Report', 'Study', 'Research', 'Paper', 'Article', 'Essay',
        'Thesis', 'Dissertation', 'Publication', 'Journal', 'Magazine', 'Newspaper', 'Newsletter',
        'Bulletin', 'Update', 'Notice', 'Announcement', 'Alert', 'Warning', 'Error', 'Success',
        'Info', 'Information', 'Details', 'Summary', 'Overview', 'Introduction', 'Preface',
        'Foreword', 'Prologue', 'Chapter', 'Section', 'Part', 'Volume', 'Issue', 'Number',
        'Date', 'Time', 'Year', 'Month', 'Day', 'Week', 'Hour', 'Minute', 'Second', 'Moment',
        'Period', 'Duration', 'Length', 'Span', 'Stretch', 'Term', 'Phase', 'Stage', 'Step',
        'Level', 'Degree', 'Grade', 'Tier', 'Layer', 'Row', 'Column', 'Cell', 'Block', 'Area',
        'Zone', 'Region', 'Sector', 'Segment', 'Division', 'Department', 'Branch', 'Office',
        'Headquarters', 'Location', 'Site', 'Place', 'Spot', 'Point', 'Position', 'Venue',
        'Facility', 'Center', 'Centre', 'Institute', 'Academy', 'School', 'College', 'University',
        'Hospital', 'Clinic', 'Lab', 'Laboratory', 'Studio', 'Workshop', 'Factory', 'Plant',
        'Mill', 'Warehouse', 'Store', 'Shop', 'Market', 'Mall', 'Outlet', 'Showroom', 'Boutique',
        'Salon', 'Spa', 'Gym', 'Fitness', 'Club', 'Society', 'Association', 'Union', 'League',
        'Federation', 'Organization', 'Organisation', 'Institution', 'Foundation', 'Trust',
        'Fund', 'Charity', 'Nonprofit', 'NGO', 'Agency', 'Bureau', 'Ministry', 'Commission',
        'Committee', 'Council', 'Board', 'Jury', 'Tribunal', 'Court', 'Chamber', 'Senate',
        'Congress', 'Parliament', 'Assembly', 'Diet', 'Knesset', 'Majlis', 'Duma', 'Rada',
        'Sejm', 'Storting', 'Riksdag', 'Eduskunta', 'Althing', 'Folketing', 'Oireachtas',
        'Bundestag', 'Bundesrat', 'Landtag'}

print('Top potential company names from ISPO page:')
print('-' * 40)
count = 0
for word, freq in c.most_common(100):
    if word not in stop and len(word) > 3 and ' ' not in word:
        print(f'{freq:3d} | {word}')
        count += 1
        if count >= 20:
            break
